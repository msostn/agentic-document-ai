============================================================
RAG EVALUATION REPORT
============================================================

Total cases: 8
  (answerable=5, unanswerable=3, returned sources=4)
Passed: 7   Failed: 1

Summary:
  - Retrieval hit-rate: 80.0% (of answerable cases)
  - Citation validity:  100.0% (of cases returning sources)
  - Correct rejection:  100.0% (of unanswerable cases)
  - Keyword heuristic:  57.14% (WEAK HEURISTIC - for reference only)

Failures (1):
------------------------------------------------------------
  [case-005] Is there a waiting period for pre-existing conditions? -> retrieval/citation failure

Per-case results:
------------------------------------------------------------
  [case-001] What is the hospitalization deductible?
    answer: The document states that the annual hospitalization deductible is five hundred d...
    retrieval_hit: YES
    citation_valid: YES
    correct_rejection: YES
    keyword_heuristic: YES (heuristic)  *(weak heuristic, not a strong metric)*

  [case-002] How much notice does the policyholder have to give to cancel the policy?
    answer: The document states that the policyholder must give thirty (30) days written not...
    retrieval_hit: YES
    citation_valid: YES
    correct_rejection: YES
    keyword_heuristic: YES (heuristic)  *(weak heuristic, not a strong metric)*

  [case-003] What is the maximum annual coverage limit?
    answer: The document states that "The maximum annual coverage limit under this policy is...
    retrieval_hit: YES
    citation_valid: YES
    correct_rejection: YES
    keyword_heuristic: YES (heuristic)  *(weak heuristic, not a strong metric)*

  [case-004] Within how many days must a claim be filed?
    answer: The document states that "Every claim must be filed within ninety (90) days of t...
    retrieval_hit: YES
    citation_valid: YES
    correct_rejection: YES
    keyword_heuristic: YES (heuristic)  *(weak heuristic, not a strong metric)*

  [case-005] Is there a waiting period for pre-existing conditions?
    answer: I couldn't find enough information in the document to answer that....
    retrieval_hit: NO
    citation_valid: NO
    correct_rejection: YES
    keyword_heuristic: NO (heuristic)  *(weak heuristic, not a strong metric)*

  [case-006] What is the weather forecast for tomorrow?
    answer: I couldn't find enough information in the document to answer that....
    retrieval_hit: NO
    citation_valid: NO
    correct_rejection: YES
    keyword_heuristic: NO (heuristic)  *(weak heuristic, not a strong metric)*

  [case-007] Who won the 2022 FIFA World Cup final?
    answer: I couldn't find enough information in the document to answer that....
    retrieval_hit: NO
    citation_valid: NO
    correct_rejection: YES
    keyword_heuristic: NO (heuristic)  *(weak heuristic, not a strong metric)*

  [case-008] 
    answer: None...
    retrieval_hit: NO
    citation_valid: NO
    correct_rejection: YES

============================================================
END OF REPORT
============================================================