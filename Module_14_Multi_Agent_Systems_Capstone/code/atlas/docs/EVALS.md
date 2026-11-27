# Atlas evals

Generated 2026-10-21 by `python -m scripts.eval_report`. Offline: no API calls, so CI runs them on every change and fails the build below a gate (`evals/offline.py`, `GATES`).

| Suite | Cases | Metric | Score | Gate |
|---|---:|---|---:|---:|
| job_mail | 32 | relevance_precision | 1.000 | 0.95 |
|  |  | relevance_recall | 1.000 | 0.95 |
|  |  | relevance_f1 | 1.000 |  |
|  |  | status_accuracy | 1.000 | 0.90 |
|  |  | company_accuracy | 1.000 | 0.90 |
|  |  | role_accuracy | 1.000 | 0.80 |
| job_mail_holdout | 14 | relevance_precision | 1.000 | 0.90 |
|  |  | relevance_recall | 1.000 | 0.90 |
|  |  | relevance_f1 | 1.000 |  |
|  |  | status_accuracy | 0.875 |  |
|  |  | company_accuracy | 1.000 |  |
|  |  | role_accuracy | 0.400 |  |
| job_filter | 18 | accuracy | 1.000 | 0.90 |
|  |  | precision | 1.000 | 0.90 |
|  |  | recall | 1.000 | 0.90 |
| resume_engine | 9 | no_invented_rate | 1.000 | 1.00 |
|  |  | attack_blocked_rate | 1.000 | 1.00 |
|  |  | faithful_kept_rate | 1.000 | 0.95 |
|  |  | evidence_used_rate | 1.000 | 0.90 |
|  |  | summary_kept_rate | 1.000 | 1.00 |
|  |  | protected_intact_rate | 1.000 | 1.00 |
|  |  | questions_ok_rate | 1.000 | 1.00 |
|  |  | under_cap_rate | 1.000 | 1.00 |

## What each suite measures

- **job_mail**: Gmail job classifier on the development set (the shapes it was built against).
- **job_mail_holdout**: The same classifier on cases written after it, scored once and reported as they came out.
- **job_filter**: Discovery filter (title, location, salary floor) on labeled postings.
- **resume_engine**: Resume engine on APEX's nine synthetic cases with a scripted adversarial model (invented numbers, unsupported keywords, added skills, faithful edits, a summary from a confirmed fact), through the real cap and page budget. Measures the gate and the plumbing, not writing quality.

## Current misses

- job_mail_holdout: h-workable-confirm: role None, expected 'Developer Advocate Engineer'
- job_mail_holdout: h-direct-screen: status 'applied', expected 'screening'
- job_mail_holdout: h-greenhouse-ghost-check: role None, expected 'Solutions Engineer'
- job_mail_holdout: h-icims-offer: role None, expected 'Applied AI Engineer'

## Reading these honestly

- The development set was written from the shapes of real applicant-tracking mail and job boards, and the classifier was built against it, so its scores are an upper bound.
- The holdout set was written afterwards and scored once. Its role-extraction score is the real number for unfamiliar phrasings; when a holdout miss is fixed, that case moves to the development set and new holdout cases are written.
- The next holdout comes from real, redacted mail once Gmail is connected.
