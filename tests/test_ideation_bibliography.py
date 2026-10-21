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
