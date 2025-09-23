import asyncio
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
import mcp.types as types

async def main():
    """Connects to the postgres-mcp server and tests the available tools."""
    async with sse_client("http://localhost:8000/sse") as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()

            print("--- Testing list_tools ---")
            tools = await session.list_tools()
            print(tools)

            print("--- Testing list_schemas ---")
            schemas = await session.call_tool("list_schemas")
            print(schemas)

            print("--- Testing list_objects ---")
            objects = await session.call_tool("list_objects", {"schema_name": "public"})
            print(objects)

            print("--- Testing execute_sql (CREATE TABLE) ---")
            create_table_result = await session.call_tool("execute_sql", {"sql": "CREATE TABLE test_table (id INT, name VARCHAR(255))"})
            print(create_table_result)

            print("--- Testing get_object_details ---")
            object_details = await session.call_tool("get_object_details", {"schema_name": "public", "object_name": "test_table"})
            print(object_details)

            print("--- Testing explain_query ---")
            explain_result = await session.call_tool("explain_query", {"sql": "SELECT * FROM test_table"})
            print(explain_result)

            print("--- Testing analyze_workload_indexes ---")
            analyze_workload_indexes_result = await session.call_tool("analyze_workload_indexes")
            print(analyze_workload_indexes_result)

            print("--- Testing analyze_query_indexes ---")
            analyze_query_indexes_result = await session.call_tool("analyze_query_indexes", {"queries": ["SELECT * FROM test_table"]})
            print(analyze_query_indexes_result)

            print("--- Testing analyze_db_health ---")
            analyze_db_health_result = await session.call_tool("analyze_db_health")
            print(analyze_db_health_result)

            print("--- Testing get_top_queries ---")
            get_top_queries_result = await session.call_tool("get_top_queries")
            print(get_top_queries_result)

            print("--- Testing execute_sql (DROP TABLE) ---")
            drop_table_result = await session.call_tool("execute_sql", {"sql": "DROP TABLE test_table"})
            print(drop_table_result)

if __name__ == "__main__":
    asyncio.run(main())