

  Next Pass

  1. Patch delete_task and extend the DAG parser, then rerun the failing tool calls to confirm they now return success.
  2. Chase the mark_task_complete exception path and fix the server so the success bubble reaches the client.
  3. Update the tool READMEs + manifest with the clarified argument/format guidance (including the task-registry prerequisite).
  4. Re-run the CLI test suite once the above lands; report back with the fresh results.

  Let me know if you want me to prioritise the documentation before the code fixes, or if I should start shipping patches now.


  - upload_dag [RENAME NEW_PROJECT] — success: no; schema clarity: unclear; note: simple YAML with an edge triggered “No edges detected”, implying a stricter format that’s not documented.
[MUCH CLEARER INSTRUCTIONS NEEDED, GATES/REQUIRMENTS, NOTES ALL MUST BE LINKED - IN GENERAL THIS MUST BE CLEARER]

WHILE YOU'RE IN THE CODE:
1) ADD A NOTES FEATURE, IT MAY ALREADY EXIST OR AT LEAST THE TABLES, "IS NOTE" COLUMN IN KB, NOT META TABLE, A REFERENCE COLUMN STORES UUIDS RELATED, THIS SHOULD BE HOW WE ADD A NOT TO ANYTHING. GENERAL NOTES ARE OK WITH NO UUID
2) EVERYTHING LIKE THIS SHOULD NOT BE A COLUMN BUT AN ENTRY IN KB WITH MET ATABLE AND FLAG, AUDIT THIS
3) WE NEED TO MAKE SURE ALL MODULES HAVE "KNOBS" TO SET ALL VARIABLES WITH DEFAULTS, NOTHING SHOULD BE HARD CODED
4) UPLOAD TO KB TOOL (DO WE HAVE ONE? UPLOAD DOCS?) NEEDS TO SPECIFY DOCUMENT, WEB (FETCH TOOL >> INGEST), OPTIONAL REFERENCES (UUID) ETC, THIS NEEDS TO BE VERY WELL DEFINED 
5) ADD TOOL HELP TOOL TO SEARCH SCOPED KB TO TOOL DOCS, EVERY TOOL OR SET OF TOOLS NEEDS A FULL README UPLOADED TO KB, MARKED TOOL DOCS