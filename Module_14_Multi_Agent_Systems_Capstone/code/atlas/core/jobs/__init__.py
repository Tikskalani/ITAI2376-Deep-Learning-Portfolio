"""Atlas -- the job search engine (R9b).

  config     config/jobs.yaml: the candidate profile, the watch list, the
             applications made before Atlas tracked them
  store      Postgres: applications, their events, discovered postings
  mail       Gmail: application confirmations, replies, invites and
             rejections become application status, rules first
  discovery  company job boards (Greenhouse, Lever, Ashby): filtered in
             code, survivors scored by Haiku against the profile
  followups  daily: nudges for silent applications, prep before interviews

Nothing here applies, sends or submits. Drafted materials wait for Trilok.
"""
