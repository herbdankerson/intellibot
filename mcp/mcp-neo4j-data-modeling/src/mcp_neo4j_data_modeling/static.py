DATA_INGEST_PROCESS = """
Follow these steps when ingesting data into Neo4j.
1. Create constraints before loading any data.
2. Load all nodes before relationships.
3. Then load relationships serially to avoid deadlocks.
"""
