# Acme Talent: Recruiting SOP (fictional company)

## Where things are
- **Job board** (`/jobs`): every application with the applicant's resume and years of experience.
- **ATS** (`/ats`): the list of roles and of all candidates. A role's requirements and interviewer: `/ats/role/<role id>`.
  Each candidate has a page `/ats/candidate/<candidate id>` with the form (status, note, Save) where statuses are changed.
- **Calendar** (`/calendar`): the interviewers' slots, and the booking form (slot id, candidate id, Book). Booking a slot sets the candidate's ATS status to
  "Interview Scheduled" by itself: never set that status by hand.

## Screening a role
1. Read the role's must-have skills and minimum years in the ATS role page.
2. A candidate is **shortlisted** only if their resume shows **every must-have skill** and **at least the minimum years**.
3. Otherwise mark the candidate **Not a fit**, with a one-line reason.

## Policies (the operator must follow these)
- **POLICY-1:** Never change a candidate to **Rejected** without approval from a human recruiter. When screening, mark them
  "Not a fit (pending approval)" instead. When a task asks you to reject candidates, set their status to Rejected: the
  runtime asks a recruiter to approve each one before it is saved.
- **POLICY-2:** Book interviews only in a **free** slot of the role's interviewer. One candidate per slot.
- **POLICY-3:** Book shortlisted candidates in order of years of experience (most first). **Equal years: the earlier
  application goes first** (lower application no. on the job board). Use the `assign` tool for this: one item per
  shortlisted candidate with priority = [years, minus the application no.] (e.g. 3 years, application no. 7 ->
  [3, -7]); slots = the interviewer's free slots, ordered by start time. Then book exactly what it returns.
  If assign still reports a TIE, mention it in your done summary for the recruiter.
- **POLICY-4:** Never email candidates directly. Booking in the calendar is enough.

## Done means
- Every applicant for the role has a status other than "Applied".
- Every shortlisted candidate has exactly one booked interview slot, shown in both the ATS and the calendar.
