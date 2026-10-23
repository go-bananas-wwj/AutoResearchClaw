"""Bibliographies come from publication records, never model-generated identities."""
import json

from researchclaw.ideation.sources import (
    Sources, citation_metadata, crossref_metadata, openalex_metadata,
)


def test_crossref_preserves_venue_authors_and_publication_affiliations():
    result=crossref_metadata({'type':'proceedings-article','container-title':['Soil &amp; Water Conference'],
        'author':[{'given':'A','family':'Researcher','affiliation':[{'name':'University A'},{'name':'Institute B'}]}]})
    assert result['venue']=='Soil & Water Conference' and result['venue_type']=='conference'
    assert result['authors']==[{'name':'A Researcher','affiliation':'University A；Institute B'}]
    assert result['institutions']==['University A','Institute B']
    assert crossref_metadata({})['authors']==[]
    assert crossref_metadata({'author':[{'given':'A','family':'B'}]})['institutions']==[]


def test_openalex_uses_journal_location_instead_of_indexing_platform():
    result=openalex_metadata({'type':'article',
        'primary_location':{'source':{'display_name':'DOAJ','type':'repository'}},
        'locations':[{'source':{'display_name':'Geomorphology','type':'journal'}}],
        'authorships':[{'author':{'id':'a1','display_name':'Author A'},'institutions':[{'display_name':'University A'},{'display_name':'Institute B'}]},
                       {'author':{'id':'a2','display_name':'Author B'},'raw_affiliation_strings':['Laboratory C']}]})
    assert result['venue']=='Geomorphology' and result['venue_type']=='journal'
    assert result['institutions']==['University A','Institute B','Laboratory C']
    assert result['authors'][0]['affiliation']=='University A；Institute B'
    missing=openalex_metadata({'authorships':[{'author':{'display_name':'Author A','last_known_institutions':['Not a publication affiliation']}}]})
    assert missing['institutions']==[]


def test_publisher_meta_handles_attribute_order_and_html_entities():
    result=citation_metadata('<meta content="Journal &amp; Science" name="citation_journal_title">'
        '<meta name="citation_author" content="Author A"><meta name="citation_author_institution" content="University A">')
    assert result['venue']=='Journal & Science' and result['venue_type']=='journal'
    assert result['authors'][0]['name']=='Author A' and result['institutions']==['University A']


def test_enrichment_only_updates_real_publication_fields(monkeypatch):
    from researchclaw.ideation import sources
    def fetch(url,limit):
        record={'type':'article','primary_location':{'source':{'display_name':'Real Journal','type':'journal'}},
            'authorships':[{'author':{'display_name':'Real Author'},'institutions':[{'display_name':'Real University'}]}]}
        return json.dumps(record).encode(),'application/json',url
    monkeypatch.setattr(sources,'fetch',fetch)
    result=Sources(None).enrich_metadata({'paper_id':'p-id','doi':'10.1234/test','title':'Paper','selected':True,'required':False})
    assert result['venue']=='Real Journal' and result['institutions']==['Real University']
    assert result['bibliography_sources'][0]['source']=='OpenAlex'
    assert not {'paper_id','title','selected','required','relevance','summary_zh'} & result.keys()


def test_fulltext_resolves_openalex_id_without_doi_and_discovers_pdf(monkeypatch,tmp_path):
    from researchclaw.ideation import sources
    from researchclaw.ideation.sources import paper_record
    calls=[]
    def fetch(url,limit=25*1024*1024,**kwargs):
        calls.append(url)
        if url.startswith('https://api.openalex.org/'):
            return json.dumps({'primary_location':{'landing_page_url':'https://journal.example/article'}}).encode(),'application/json',url
        if url=='https://journal.example/article':
            return b'<meta content="/paper.pdf" name="citation_pdf_url">','text/html',url
        if url=='https://journal.example/paper.pdf':return b'%PDF fixture','application/pdf',url
        return b'Index only','text/html',url
    monkeypatch.setattr(sources,'fetch',fetch)
    monkeypatch.setattr(Sources,'pdf_document',staticmethod(lambda path:{'pages':[{'location':'page 2','text':'LS factor calculation and validation. Methods: terrain routing and validation. '*100}]}))
    p=paper_record({'title':'LS factor calculation and validation','url':'https://openalex.org/W123'})
    doc=Sources(None).fulltext(p,tmp_path)
    assert calls[0]=='https://api.openalex.org/works/W123'
    assert doc['source_url']=='https://journal.example/paper.pdf'
    assert p['fetch_attempts'][-1]['outcome']=='已取得方法正文'


def test_content_auth_is_not_stored_or_forwarded(monkeypatch):
    from researchclaw.ideation import sources
    requests=[]
    monkeypatch.setenv('OPENALEX_API_KEY','secret-fixture-never-store')
    monkeypatch.setattr(sources,'check_url_ssrf',lambda url:None)
    class Response:
        headers={'Content-Type':'application/pdf'}
        url='https://storage.example/doc.pdf?signature=hidden'
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,size):return b'%PDF test'
    class Opener:
        def open(self,req,timeout):requests.append(req);return Response()
    monkeypatch.setattr(sources.urllib.request,'build_opener',lambda *args:Opener())
    _,_,url=sources.fetch('https://content.openalex.org/works/W123.pdf')
    assert url=='https://storage.example/doc.pdf'
    req=requests[0]
    assert 'api_key=secret-fixture-never-store' in req.full_url
    assert 'Authorization' not in req.headers and 'Authorization' in req.unredirected_hdrs
    redirected=sources.PublicRedirect().redirect_request(req,None,302,'Found',{},'https://storage.example/doc.pdf')
    assert not redirected.has_header('Authorization') and 'secret-fixture' not in redirected.full_url
    sources.fetch('https://journal.example/paper.pdf')
    assert not requests[-1].has_header('Authorization') and 'api_key' not in requests[-1].full_url


def test_missing_content_key_and_http_failure_are_distinct(monkeypatch,tmp_path):
    import urllib.error
    from researchclaw.ideation import sources
    from researchclaw.ideation.sources import paper_record
    from researchclaw.ideation.store import SessionError
    import pytest
    monkeypatch.delenv('OPENALEX_API_KEY',raising=False)
    def fetch(url,limit=25*1024*1024,**kwargs):
        if 'api.openalex.org' in url:
            return json.dumps({'content_urls':{'pdf':'https://content.openalex.org/works/W123.pdf'},
                'primary_location':{'pdf_url':'https://publisher.example/paper.pdf'}}).encode(),'application/json',url
        raise urllib.error.HTTPError(url,403,'Forbidden',{},None)
    monkeypatch.setattr(sources,'fetch',fetch)
    p=paper_record({'title':'Paper','doi':'10.1234/test'})
    with pytest.raises(SessionError,match='OPENALEX_API_KEY.*访问被拒绝'):
        Sources(None).fulltext(p,tmp_path)
    assert {a['outcome'] for a in p['fetch_attempts']}=={'OpenAlex缓存全文需要配置OPENALEX_API_KEY','访问被拒绝'}


def test_fetch_encodes_unicode_publisher_links_without_leaking_key(monkeypatch):
    from researchclaw.ideation import sources
    monkeypatch.setenv('OPENALEX_API_KEY','private-test-key')
    monkeypatch.setattr(sources,'check_url_ssrf',lambda url:None)
    class Reply:
        headers={'Content-Type':'text/html'}
        url='https://journal.example/article'
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,size):return b'paper'
    class Opener:
        def open(self,req,timeout):
            assert '%CE%BB' in req.full_url and '%E5%9D%A1' in req.full_url
            assert not req.has_header('Authorization') and 'private-test-key' not in req.full_url
            return Reply()
    monkeypatch.setattr(sources.urllib.request,'build_opener',lambda *args:Opener())
    sources.fetch('https://journal.example/slope-λ?topic=坡长')


def test_openalex_classic_and_citation_search_use_real_distinct_routes(monkeypatch):
    from urllib.parse import urlparse,parse_qs
    from researchclaw.ideation import sources
    from researchclaw.literature import cache
    monkeypatch.setattr(cache,'get_cached',lambda *a,**kw:None)
    monkeypatch.setattr(cache,'put_cache',lambda *a,**kw:None)
    calls=[]
    def work(wid,title,year,citations=1):
        return {'id':'https://openalex.org/'+wid,'title':title,'publication_year':year,'cited_by_count':citations,'doi':'https://doi.org/10.1234/'+wid,'authorships':[]}
    def fetch(url,*args,**kwargs):
        calls.append(url);query=parse_qs(urlparse(url).query)
        if query.get('search'):
            return json.dumps({'results':[work('W11','Classic method',1990,500)]}).encode(),'application/json',url
        if query.get('filter',[''])[0].startswith('openalex:'):
            records=[work('W11','Classic method',1990,500)]
        elif query.get('filter',[''])[0].startswith('cites:'):
            records=[work('W12','Recent continuation',2026)]
        else:
            return json.dumps({'id':'https://openalex.org/W10','referenced_works':['https://openalex.org/W11']}).encode(),'application/json',url
        return json.dumps({'results':records}).encode(),'application/json',url
    monkeypatch.setattr(sources,'fetch',fetch)
    client=sources.Sources(None)
    classic=client.search('core mechanism',period='classic',source='openalex')
    assert classic[0]['year']==1990 and classic[0]['discovery_relation']=='classic_search'
    assert parse_qs(urlparse(calls[0]).query)['sort']==['cited_by_count:desc']
    assert 'publication_date' not in calls[0]
    seed=sources.paper_record({'title':'Seed method','doi':'10.1234/seed'})
    found=client.citation_neighbors(seed)
    assert [p['discovery_relation'] for p in found]==['reference','citing']
    assert all(p['discovery_seed_id']==seed['paper_id'] and p['relevance']=='unreviewed' for p in found)


def test_html_abstract_and_reference_landing_is_not_fulltext():
    from researchclaw.ideation.sources import readable_document
    landing={'pages':[{'location':'HTML正文','text':'# Paper title\n## Abstract\n'+('Methods are discussed in this abstract. '*90)+'\n## References\n'+('Study methods reference. '*100)}]}
    assert not readable_document(landing)
    full={'pages':[{'location':'HTML正文','text':'# Paper title\n## 1 Introduction\n'+('The study context and physical question. '*40)+'\n## 2 Methods\n'+('The actual method and controls are described here. '*40)+'\n## 3 Results\n'+('Independent observations validate the stated mechanism. '*40)}]}
    assert readable_document(full)


def test_pdf_of_a_reference_cannot_be_attached_to_the_requested_paper():
    from researchclaw.ideation.sources import document_matches_paper
    paper={'title':'Probabilistic landslide time-of-failure prediction','doi':'10.1007/landslide-test'}
    wrong={'pages':[{'location':'第 1 页','text':'A Density-Based Algorithm for Discovering Clusters. DBSCAN method. '*60}]}
    assert not document_matches_paper(paper,wrong)
    right={'pages':[{'location':'第 1 页','text':'Probabilistic landslide time-of-failure prediction\nMethods and independent observations. '*60}]}
    assert document_matches_paper(paper,right)


def test_user_uploaded_pdf_identity_can_use_server_bound_content_hash():
    from researchclaw.ideation.sources import document_matches_paper
    paper={'title':'paper.pdf','uploaded_pdf':'uploads/paper.pdf','content_hash':'verified-upload-sha'}
    doc={'upload_sha256':'verified-upload-sha','pages':[{'location':'第 1 页','text':'Methods and actual original observations. '*80}]}
    assert document_matches_paper(paper,doc)
    doc['upload_sha256']='different-upload-sha'
    assert not document_matches_paper(paper,doc)
