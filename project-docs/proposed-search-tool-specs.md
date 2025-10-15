Search Tool:
1) Uses Startpage through my SearXNG instance (google isn't working right now) to pull N results against a search
2) Checks the urls against the urls in the web table, upserts urls and meta data
3) Runs encoding on snippets/summaries
4) Search and re ranks against the table
5) Top X results are fetched, converted to .md and sent to an LLM (Qwen 3 local for now) for summarization and assesment against the query, the llm summarizes the contents and gives a relevance score or recommendation
6) responses are stored in an additional column or meta table and embedded - need to make sure there's not any "collisions" here - how do we currently connect embeddings to their source? how will this work if its in the same column? it may need its own table with the entry referenced by uuid to this table so we can separate the tracking uuid - I'm entirely blind here, I have no idea how these relate
7) a vector/bm25 search is ran against the new responses and results are reranked
8) return top Y results raw .md contents and ingest in kb, add column to kb is web and track the kb uuid in the web table when it gets inserted to kb so we can track meta data