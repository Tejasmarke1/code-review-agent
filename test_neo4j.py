import sys
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv()
from src.memory.neo4j_client import Neo4jClient

c = Neo4jClient.get_instance()
ok = c.connect()
print("Connected:", ok)
if ok:
    c.initialize_schema()
    print("Schema ready")