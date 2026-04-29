"""Knowledge base: index GitHub repos into ChromaDB for RAG."""

import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path

import chromadb
from git import Repo

logger = logging.getLogger(__name__)

RELEVANT_EXTENSIONS = {
    ".py", ".sql", ".md", ".yaml", ".yml", ".toml", ".json", ".txt",
    ".sh", ".tf", ".cfg", ".ini", ".rst",
}
MAX_FILE_SIZE = 50_000  # chars


class KnowledgeBase:
    def __init__(self, persist_dir: str = ".chroma"):
        self.client = chromadb.PersistentClient(path=persist_dir)

    def _collection_for_user(self, slack_user_id: str) -> chromadb.Collection:
        safe_name = f"user_{slack_user_id.lower()}"
        return self.client.get_or_create_collection(name=safe_name)

    def index_repo(self, slack_user_id: str, repo_url: str, github_token: str | None = None):
        """Clone and index a GitHub repo for a user."""
        collection = self._collection_for_user(slack_user_id)

        # Build authenticated URL
        if github_token and "github.com" in repo_url:
            repo_url = repo_url.replace(
                "https://github.com/",
                f"https://{github_token}@github.com/",
            )

        tmpdir = tempfile.mkdtemp()
        try:
            logger.info(f"Cloning {repo_url} ...")
            repo = Repo.clone_from(repo_url, tmpdir, depth=1)

            documents = []
            metadatas = []
            ids = []

            for file_path in Path(tmpdir).rglob("*"):
                if not file_path.is_file():
                    continue
                if file_path.suffix not in RELEVANT_EXTENSIONS:
                    continue
                if any(part.startswith(".") for part in file_path.parts):
                    continue

                try:
                    content = file_path.read_text(errors="ignore")
                except Exception:
                    continue

                if len(content) > MAX_FILE_SIZE:
                    content = content[:MAX_FILE_SIZE]

                rel_path = str(file_path.relative_to(tmpdir))
                doc_id = hashlib.md5(f"{repo_url}:{rel_path}".encode()).hexdigest()

                # Chunk large files
                chunks = self._chunk_text(content, rel_path)
                for i, chunk in enumerate(chunks):
                    chunk_id = f"{doc_id}_{i}"
                    documents.append(chunk)
                    metadatas.append({"repo": repo_url, "path": rel_path, "chunk": i})
                    ids.append(chunk_id)

            if documents:
                # Upsert in batches
                batch_size = 100
                for i in range(0, len(documents), batch_size):
                    collection.upsert(
                        documents=documents[i:i + batch_size],
                        metadatas=metadatas[i:i + batch_size],
                        ids=ids[i:i + batch_size],
                    )
                logger.info(f"Indexed {len(documents)} chunks from {repo_url}")
            else:
                logger.warning(f"No indexable files found in {repo_url}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def query(self, slack_user_id: str, question: str, n_results: int = 5) -> list[dict]:
        """Query the knowledge base for relevant context."""
        collection = self._collection_for_user(slack_user_id)
        if collection.count() == 0:
            return []

        results = collection.query(query_texts=[question], n_results=n_results)
        context = []
        for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
            context.append({
                "content": doc,
                "repo": meta.get("repo", ""),
                "path": meta.get("path", ""),
            })
        return context

    @staticmethod
    def _chunk_text(text: str, path: str, chunk_size: int = 2000, overlap: int = 200) -> list[str]:
        """Split text into overlapping chunks with file path prefix."""
        if len(text) <= chunk_size:
            return [f"# {path}\n{text}"]

        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunk = text[start:end]
            chunks.append(f"# {path}\n{chunk}")
            start = end - overlap
        return chunks
