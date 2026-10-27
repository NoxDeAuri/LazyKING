# Submissions Agent: Project Spec

Owner: Trevor Trosclair
Runs in: Claude Code session on Mac (zsh)
Status: Draft for first Claude Code session

## Goal

An MCP server plus supporting scripts that:

1. Keeps a profile of Trevor's written works (short stories and the novel "Creme Brulee").
2. Searches submission sites on command and matches open calls to current works.
3. Finds boutique options (calls under 5,000 words with at least 2 weeks before deadline) and presents them.
4. Builds a timeline for accepted works from a routine Trevor provides, and recalculates when he reports running faster or slower.
5. Tracks Instagram activity through manual input and suggests captions, image types, and schedule changes, using the schedule from another Claude thread as a baseline.

## Constraints

- No API for Instagram. Trevor pastes post details or insight screenshots; the agent works from that.
- Logins for submission sites come from Bitwarden. Trevor enters the master password manually at run time.
- Trevor starts every check manually. Nothing runs on a schedule.
- The agent never submits a story or query without explicit approval for that item.
- Secrets never go in source files or git.

## Secrets handling

- `.env` (gitignored) holds:
  - `BW_CLIENTID`, `BW_CLIENTSECRET` (Bitwarden personal API key)
  - `TWEEK_API_KEY`
- Master password is read with hidden terminal input at startup. It is never a command-line argument and never written to disk.
- Bitwarden flow: `bw login --apikey`, then `bw unlock` with the master password via stdin or `--passwordenv` for that process only. Pass the session key to child processes; don't write it to a file.
- Add `.env`, `works.json`, `*.session`, and `data/` to `.gitignore`.

## Project layout

```
submissions-agent/
  SPEC.md
  .env                 (gitignored)
  .gitignore
  pyproject.toml or package.json
  src/
    server.py          MCP server, tool registration
    profile/
      builder.py       interactive works-profile builder
      works.json       saved profiles (gitignored)
    secrets/
      bitwarden.py     login, unlock, fetch credentials for a site
    sites/
      chillsubs.py     browser-driven search and filter
      base.py          shared listing schema
    match/
      matcher.py       match works to listings, boutique finder
    timeline/
      builder.py       accepted-work timelines from routine
      tweek.py         Tweek API client (create tasks, checklists)
    instagram/
      log.py           manual post log, schedule comparison, suggestions
  data/                (gitignored) listings cache, tracker
  tests/
```

## Works profile builder

An interactive command that walks Trevor through each work and saves a record. Run it once per work and again when something changes.

Fields per work:

- `id`, `title`, `type` (short story / novel), `status` (drafting / complete / submitted / accepted / published)
- `word_count` (number, not estimate)
- `genre`, `themes` (list)
- `synopsis` (short and long)
- `comps` (optional)
- `content_flags` (anything a call might exclude, e.g., "contains violence")
- `submission_history` (list of call, date, result)
- `accepted_on`, `timeline_id` (filled when accepted)

Behavior:

- Asks one question at a time and writes after each answer.
- Shows the record before saving. Trevor confirms.
- Never invents synopsis text. If Trevor wants help drafting, the agent proposes a draft and waits for approval.
- Updates an existing record when word count or status changes.

## MCP tools (first version)

| Tool | Purpose |
|---|---|
| `list_works` | Return all works with status and word count |
| `upsert_work` | Add or update a work (used by the builder) |
| `search_calls` | Run Chill Subs search with filters, return listings |
| `match_works` | Match current works to listings, with reasons |
| `find_boutique` | Return calls under 5,000 words with at least 14 days to deadline |
| `record_submission` | Log a submission after Trevor approves it |
| `create_timeline` | Build a timeline for an accepted work from a routine |
| `adjust_timeline` | Recalculate remaining dates from a "faster/slower" update |
| `log_instagram_post` | Save a post Trevor pastes or describes |
| `suggest_instagram` | Suggest captions, image types, and schedule changes |

## Site integration

**Chill Subs (first site).** Browser-driven, not raw HTTP. Use the built-in browser or Playwright with Trevor's signed-in session. Bitwarden item: `chillsubs.com`.

Left-side sections Trevor wants searched: **Submission Calls**, **Magazines**, **Presses**.

Filters, in order:

1. Genre: Fiction
2. Subgenre (to be confirmed on the site)
3. Length: flash fiction, micro fiction, or short stories

Steps:

1. Confirm what the listing page shows and how filters work. Record the selectors and the fields available: genre, word count, deadline, reading period, fee, simultaneous submissions.
2. Extract into the shared listing schema.
3. Cache results in `data/` with a timestamp.

Open question: whether Chill Subs renders listings in a way a browser can read cleanly. Verify before writing extraction logic.

**Duotrope / QueryTracker (later).** Add after Chill Subs works. Each needs a site module in `sites/`.

## Timeline builder

Routine (confirmed by Trevor):

- Writes 5 to 7 days a week.
- Draft 1 is handwritten in a notebook. Target: 2 pages per session. Track pages, not words.
- Drafts 2 and later are typed. Target: at least 1,000 words per session. Track words.
- Drafts 3 and 4 each take one night at most. Each draft should take less time than the one before.
- Goal: a work is ready to submit after draft 4.

Timeline logic:

- Draft 1: pages per session × sessions per week → estimated completion date. Store the words-per-page estimate in `works.json` so draft 2 can be sized from the handwritten length. Trevor's estimate: 500 words per handwritten page, so 2 pages per session ≈ 1,000 words per session, which matches the draft 2 target.
- Draft 2: word count ÷ 1,000 words per session → session count → dates.
- Drafts 3 and 4: fixed one-night sessions, scheduled after draft 2 ends, with the time budget shrinking per draft.
- Each draft task includes a checklist in Tweek (or a local file), with the word or page target for that session.

Output: dated tasks, written to Tweek as tasks with checklists, and to a local file for reference.

Adjust: when Trevor reports "slower" or "faster," recalculate remaining dates from today, keep completed tasks, and show the change before saving.

Tweek: confirm the API endpoints against Tweek's API docs at tweek.so before writing the client. Start with read-only calls to confirm auth works.

## Instagram

- No automated reading. Trevor pastes post details or insight numbers.
- `log_instagram_post` stores date, format, caption, and any numbers he provides.
- `suggest_instagram` compares the log against the schedule from the other thread (Trevor pastes it in as `data/schedule.md`) and suggests changes. It flags gaps but doesn't rewrite Trevor's voice without approval.

## Build order

1. Project scaffold, `.gitignore`, `.env` loader, Bitwarden login/unlock with hidden password prompt. Test with a dummy item.
2. Works profile builder and `list_works` / `upsert_work`. Test with two works.
3. Chill Subs investigation: record what's visible, which filters exist, and what fields a listing shows. Write findings to `data/chillsubs-notes.md`.
4. `search_calls` and `match_works` for Chill Subs. Include the reasons for each match.
5. `find_boutique`.
6. Timeline builder with `create_timeline` and `adjust_timeline`. Tweek client last.
7. Instagram log and suggestions.
8. MCP wiring for all tools. Test each tool from Claude Code.

## Open questions for Trevor

1. Bitwarden item for Chill Subs: `chillsubs.com` (confirmed).
2. Do you want the timeline output in Tweek, a local file, or both?
3. Routine captured: 5–7 days/week; draft 1 handwritten at 2 pages/session; drafts 2+ at 1,000+ words/session; drafts 3–4 one night max each. Words per handwritten page: 500 (confirmed). Draft 1 at 2 pages/session ≈ 1,000 words/session, consistent with the draft 2 target.
4. Paste the Instagram schedule from the other thread into `data/schedule.md` when we reach step 7.
5. Should the agent drop a call when the listing has no reading period or fee, or flag it?

## Definition of done (first version)

- Works profile builder saves and edits works without corrupting `works.json`.
- Chill Subs search returns listings with the shared schema and the reasons for each match.
- `find_boutique` returns only qualifying calls, with the deadline and word limit shown.
- Every submission requires explicit approval and is logged.
- No secrets in git history. `.env` is gitignored and verified.
