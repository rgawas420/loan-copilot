"""Load data/sample_kb into the vector store (needed once when using Pinecone).
Chunk ids are stable, so running it again overwrites instead of duplicating."""
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.rag.vectorstore import add_documents
from app.services.ingestion import load_directory

if __name__ == "__main__":
    setup_logging()
    s = get_settings()
    if not s.pinecone_api_key:
        print("PINECONE_API_KEY is empty: the app builds an in-memory KB at startup, nothing to ingest.")
    else:
        chunks = load_directory(s.kb_dir)
        ids = [f"kb-{c.metadata['source']}-{i}" for i, c in enumerate(chunks)]
        print(f"Indexed {add_documents(chunks, ids=ids)} chunks into '{s.pinecone_index_name}/{s.pinecone_namespace}'.")
