"""Durable run execution outside the Web process (local adaptation).

``researchclaw.worker.supervisor`` runs as the ``rc-worker`` service, launches
one child process per queued job and resumes interrupted runs from their
checkpoint.  See docs/AUTORESEARCH_ARCHITECTURE_CN.md §3.1.
"""
