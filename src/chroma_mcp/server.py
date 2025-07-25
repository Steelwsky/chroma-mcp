from typing import Dict, List, Mapping, Optional, TypedDict, Union, Set
from enum import Enum
import chromadb
from mcp.server.fastmcp import FastMCP
import os
from dotenv import load_dotenv
import argparse
from chromadb.config import Settings
import ssl
import uuid
import time
import json
import pathlib
import zipfile
import tarfile
import rarfile
import shutil
import tempfile
import pandas as pd
from sentence_transformers import SentenceTransformer
import py7zr
import chardet


from chromadb.api.collection_configuration import (
    CreateCollectionConfiguration
)
from chromadb.api.types import EmbeddingFunction, GetResult
from chromadb.utils.embedding_functions import (
    DefaultEmbeddingFunction,
    CohereEmbeddingFunction,
    OpenAIEmbeddingFunction,
    JinaEmbeddingFunction,
    VoyageAIEmbeddingFunction,
    RoboflowEmbeddingFunction,
)

# Initialize FastMCP server
mcp = FastMCP("chroma")

# Global variables
_chroma_client = None


def create_parser():
    """Create and return the argument parser."""
    parser = argparse.ArgumentParser(
        description='FastMCP server for Chroma DB')
    parser.add_argument('--client-type',
                        choices=['http', 'cloud', 'persistent', 'ephemeral'],
                        default=os.getenv('CHROMA_CLIENT_TYPE', 'ephemeral'),
                        help='Type of Chroma client to use')
    parser.add_argument('--data-dir',
                        default=os.getenv('CHROMA_DATA_DIR'),
                        help='Directory for persistent client data (only used with persistent client)')
    parser.add_argument('--host',
                        help='Chroma host (required for http client)',
                        default=os.getenv('CHROMA_HOST'))
    parser.add_argument('--port',
                        help='Chroma port (optional for http client)',
                        default=os.getenv('CHROMA_PORT'))
    parser.add_argument('--custom-auth-credentials',
                        help='Custom auth credentials (optional for http client)',
                        default=os.getenv('CHROMA_CUSTOM_AUTH_CREDENTIALS'))
    parser.add_argument('--tenant',
                        help='Chroma tenant (optional for http client)',
                        default=os.getenv('CHROMA_TENANT'))
    parser.add_argument('--database',
                        help='Chroma database (required if tenant is provided)',
                        default=os.getenv('CHROMA_DATABASE'))
    parser.add_argument('--api-key',
                        help='Chroma API key (required if tenant is provided)',
                        default=os.getenv('CHROMA_API_KEY'))
    parser.add_argument('--ssl',
                        help='Use SSL (optional for http client)',
                        type=lambda x: x.lower() in [
                            'true', 'yes', '1', 't', 'y'],
                        default=os.getenv('CHROMA_SSL', 'true').lower() in ['true', 'yes', '1', 't', 'y'])
    parser.add_argument('--dotenv-path',
                        help='Path to .env file',
                        default=os.getenv('CHROMA_DOTENV_PATH', '.chroma_env'))
    return parser


def get_chroma_client(args=None):
    """Get or create the global Chroma client instance."""
    global _chroma_client
    if _chroma_client is None:
        if args is None:
            # Create parser and parse args if not provided
            parser = create_parser()
            args = parser.parse_args()

        # Load environment variables from .env file if it exists
        load_dotenv(dotenv_path=args.dotenv_path)
        if args.client_type == 'http':
            if not args.host:
                raise ValueError(
                    "Host must be provided via --host flag or CHROMA_HOST environment variable when using HTTP client")

            settings = Settings()
            if args.custom_auth_credentials:
                settings = Settings(
                    chroma_client_auth_provider="chromadb.auth.basic_authn.BasicAuthClientProvider",
                    chroma_client_auth_credentials=args.custom_auth_credentials
                )

            # Handle SSL configuration
            try:
                _chroma_client = chromadb.HttpClient(
                    host=args.host,
                    port=args.port if args.port else 8000,
                    ssl=args.ssl,
                    settings=settings
                )
            except ssl.SSLError as e:
                print(f"SSL connection failed: {str(e)}")
                raise
            except Exception as e:
                print(f"Error connecting to HTTP client: {str(e)}")
                raise

        elif args.client_type == 'cloud':
            if not args.tenant:
                raise ValueError(
                    "Tenant must be provided via --tenant flag or CHROMA_TENANT environment variable when using cloud client")
            if not args.database:
                raise ValueError(
                    "Database must be provided via --database flag or CHROMA_DATABASE environment variable when using cloud client")
            if not args.api_key:
                raise ValueError(
                    "API key must be provided via --api-key flag or CHROMA_API_KEY environment variable when using cloud client")

            try:
                _chroma_client = chromadb.HttpClient(
                    host="api.trychroma.com",
                    ssl=True,  # Always use SSL for cloud
                    tenant=args.tenant,
                    database=args.database,
                    headers={
                        'x-chroma-token': args.api_key
                    }
                )
            except ssl.SSLError as e:
                print(f"SSL connection failed: {str(e)}")
                raise
            except Exception as e:
                print(f"Error connecting to cloud client: {str(e)}")
                raise

        elif args.client_type == 'persistent':
            if not args.data_dir:
                raise ValueError(
                    "Data directory must be provided via --data-dir flag when using persistent client")
            _chroma_client = chromadb.PersistentClient(path=args.data_dir)
        else:  # ephemeral
            _chroma_client = chromadb.EphemeralClient()

    return _chroma_client

##### Collection Tools #####


@mcp.tool()
async def chroma_list_collections(
    limit: int | None = None,
    offset: int | None = None
) -> List[str]:
    """List all collection names in the Chroma database with pagination support.

    Args:
        limit: Optional maximum number of collections to return
        offset: Optional number of collections to skip before returning results

    Returns:
        List of collection names or ["__NO_COLLECTIONS_FOUND__"] if database is empty
    """
    client = get_chroma_client()
    try:
        colls = client.list_collections(limit=limit, offset=offset)
        # Safe handling: If colls is None or empty, return a special marker
        if not colls:
            return ["__NO_COLLECTIONS_FOUND__"]
        # Otherwise iterate to get collection names
        return [coll.name for coll in colls]

    except Exception as e:
        raise Exception(f"Failed to list collections: {str(e)}") from e

mcp_known_embedding_functions: Dict[str, type[EmbeddingFunction]] = {
    "default": DefaultEmbeddingFunction,
    "cohere": CohereEmbeddingFunction,
    "openai": OpenAIEmbeddingFunction,
    "jina": JinaEmbeddingFunction,
    "voyageai": VoyageAIEmbeddingFunction,
    "roboflow": RoboflowEmbeddingFunction,
}


@mcp.tool()
async def chroma_create_collection(
    collection_name: str,
    embedding_function_name: str = "default",
    metadata: Dict | None = None,
) -> str:
    """Create a new Chroma collection with configurable HNSW parameters.

    Args:
        collection_name: Name of the collection to create
        embedding_function_name: Name of the embedding function to use. Options: 'default', 'cohere', 'openai', 'jina', 'voyageai', 'ollama', 'roboflow'
        metadata: Optional metadata dict to add to the collection
    """
    client = get_chroma_client()

    embedding_function = mcp_known_embedding_functions[embedding_function_name]

    configuration = CreateCollectionConfiguration(
        embedding_function=embedding_function()
    )

    try:
        client.create_collection(
            name=collection_name,
            configuration=configuration,
            metadata=metadata
        )
        config_msg = f" with configuration: {configuration}"
        return f"Successfully created collection {collection_name}{config_msg}"
    except Exception as e:
        raise Exception(
            f"Failed to create collection '{collection_name}': {str(e)}") from e


@mcp.tool()
async def chroma_peek_collection(
    collection_name: str,
    limit: int = 5
) -> Dict:
    """Peek at documents in a Chroma collection.

    Args:
        collection_name: Name of the collection to peek into
        limit: Number of documents to peek at
    """
    client = get_chroma_client()
    try:
        collection = client.get_collection(collection_name)
        results = collection.peek(limit=limit)
        return results
    except Exception as e:
        raise Exception(
            f"Failed to peek collection '{collection_name}': {str(e)}") from e


@mcp.tool()
async def chroma_get_collection_info(collection_name: str) -> Dict:
    """Get information about a Chroma collection.

    Args:
        collection_name: Name of the collection to get info about
    """
    client = get_chroma_client()
    try:
        collection = client.get_collection(collection_name)

        # Get collection count
        count = collection.count()

        # Peek at a few documents
        peek_results = collection.peek(limit=3)

        return {
            "name": collection_name,
            "count": count,
            "sample_documents": peek_results
        }
    except Exception as e:
        raise Exception(
            f"Failed to get collection info for '{collection_name}': {str(e)}") from e


@mcp.tool()
async def chroma_get_collection_count(collection_name: str) -> int:
    """Get the number of documents in a Chroma collection.

    Args:
        collection_name: Name of the collection to count
    """
    client = get_chroma_client()
    try:
        collection = client.get_collection(collection_name)
        return collection.count()
    except Exception as e:
        raise Exception(
            f"Failed to get collection count for '{collection_name}': {str(e)}") from e


@mcp.tool()
async def chroma_modify_collection(
    collection_name: str,
    new_name: str | None = None,
    new_metadata: Dict | None = None,
) -> str:
    """Modify a Chroma collection's name or metadata.

    Args:
        collection_name: Name of the collection to modify
        new_name: Optional new name for the collection
        new_metadata: Optional new metadata for the collection
    """
    client = get_chroma_client()
    try:
        collection = client.get_collection(collection_name)
        collection.modify(name=new_name, metadata=new_metadata)

        modified_aspects = []
        if new_name:
            modified_aspects.append("name")
        if new_metadata:
            modified_aspects.append("metadata")

        return f"Successfully modified collection {collection_name}: updated {' and '.join(modified_aspects)}"
    except Exception as e:
        raise Exception(
            f"Failed to modify collection '{collection_name}': {str(e)}") from e


@mcp.tool()
async def chroma_delete_collection(collection_name: str) -> str:
    """Delete a Chroma collection.

    Args:
        collection_name: Name of the collection to delete
    """
    client = get_chroma_client()
    try:
        client.delete_collection(collection_name)
        return f"Successfully deleted collection {collection_name}"
    except Exception as e:
        raise Exception(
            f"Failed to delete collection '{collection_name}': {str(e)}") from e

##### Document Tools #####


@mcp.tool()
async def chroma_add_documents(
    collection_name: str,
    documents: List[str],
    ids: List[str],
    metadatas: Optional[List[Dict]] = None,
) -> str:
    """Add documents to a Chroma collection.

    Args:
        collection_name: Name of the collection to add documents to
        documents: List of text documents to add
        ids: List of IDs for the documents (required)
        metadatas: Optional list of metadata dictionaries for each document
    """
    if not documents:
        raise ValueError("The 'documents' list cannot be empty.")

    if not ids:
        raise ValueError("The 'ids' list is required and cannot be empty.")

    # Check if there are empty strings in the ids list
    if any(not id.strip() for id in ids):
        raise ValueError("IDs cannot be empty strings.")

    if len(ids) != len(documents):
        raise ValueError(
            f"Number of ids ({len(ids)}) must match number of documents ({len(documents)}).")

    client = get_chroma_client()
    try:
        collection = client.get_or_create_collection(collection_name)

        # Check for duplicate IDs
        existing_ids = collection.get(include=[])["ids"]
        duplicate_ids = [id for id in ids if id in existing_ids]

        if duplicate_ids:
            raise ValueError(
                f"The following IDs already exist in collection '{collection_name}': {duplicate_ids}. "
                f"Use 'chroma_update_documents' to update existing documents."
            )

        result = collection.add(
            documents=documents,
            metadatas=metadatas,
            ids=ids
        )

        # Check the return value
        if result and isinstance(result, dict):
            # If the return value is a dictionary, it may contain success information
            if 'success' in result and not result['success']:
                raise Exception(
                    f"Failed to add documents: {result.get('error', 'Unknown error')}")

            # If the return value contains the actual number added
            if 'count' in result:
                return f"Successfully added {result['count']} documents to collection {collection_name}"

        # Default return
        return f"Successfully added {len(documents)} documents to collection {collection_name}, result is {result}"
    except Exception as e:
        raise Exception(
            f"Failed to add documents to collection '{collection_name}': {str(e)}") from e


@mcp.tool()
async def chroma_query_documents(
    collection_name: str,
    query_texts: List[str],
    n_results: int = 5,
    where: Dict | None = None,
    where_document: Dict | None = None,
    include: List[str] = ["documents", "metadatas", "distances"]
) -> Dict:
    """Query documents from a Chroma collection with advanced filtering.

    Args:
        collection_name: Name of the collection to query
        query_texts: List of query texts to search for
        n_results: Number of results to return per query
        where: Optional metadata filters using Chroma's query operators
               Examples:
               - Simple equality: {"metadata_field": "value"}
               - Comparison: {"metadata_field": {"$gt": 5}}
               - Logical AND: {"$and": [{"field1": {"$eq": "value1"}}, {"field2": {"$gt": 5}}]}
               - Logical OR: {"$or": [{"field1": {"$eq": "value1"}}, {"field1": {"$eq": "value2"}}]}
        where_document: Optional document content filters
        include: List of what to include in response. By default, this will include documents, metadatas, and distances.
    """
    if not query_texts:
        raise ValueError("The 'query_texts' list cannot be empty.")

    client = get_chroma_client()
    try:
        collection = client.get_collection(collection_name)
        return collection.query(
            query_texts=query_texts,
            n_results=n_results,
            where=where,
            where_document=where_document,
            include=include
        )
    except Exception as e:
        raise Exception(
            f"Failed to query documents from collection '{collection_name}': {str(e)}") from e


@mcp.tool()
async def chroma_get_documents(
    collection_name: str,
    ids: List[str] | None = None,
    where: Dict | None = None,
    where_document: Dict | None = None,
    include: List[str] = ["documents", "metadatas"],
    limit: int | None = None,
    offset: int | None = None
) -> Dict:
    """Get documents from a Chroma collection with optional filtering.

    Args:
        collection_name: Name of the collection to get documents from
        ids: Optional list of document IDs to retrieve
        where: Optional metadata filters using Chroma's query operators
               Examples:
               - Simple equality: {"metadata_field": "value"}
               - Comparison: {"metadata_field": {"$gt": 5}}
               - Logical AND: {"$and": [{"field1": {"$eq": "value1"}}, {"field2": {"$gt": 5}}]}
               - Logical OR: {"$or": [{"field1": {"$eq": "value1"}}, {"field1": {"$eq": "value2"}}]}
        where_document: Optional document content filters
        include: List of what to include in response. By default, this will include documents, and metadatas.
        limit: Optional maximum number of documents to return
        offset: Optional number of documents to skip before returning results

    Returns:
        Dictionary containing the matching documents, their IDs, and requested includes
    """
    client = get_chroma_client()
    try:
        collection = client.get_collection(collection_name)
        return collection.get(
            ids=ids,
            where=where,
            where_document=where_document,
            include=include,
            limit=limit,
            offset=offset
        )
    except Exception as e:
        raise Exception(
            f"Failed to get documents from collection '{collection_name}': {str(e)}") from e


@mcp.tool()
async def chroma_update_documents(
    collection_name: str,
    ids: List[str],
    embeddings: List[List[float]] | None = None,
    metadatas: List[Dict] | None = None,
    documents: List[str] | None = None
) -> str:
    """Update documents in a Chroma collection.

    Args:
        collection_name: Name of the collection to update documents in
        ids: List of document IDs to update (required)
        embeddings: Optional list of new embeddings for the documents.
                    Must match length of ids if provided.
        metadatas: Optional list of new metadata dictionaries for the documents.
                   Must match length of ids if provided.
        documents: Optional list of new text documents.
                   Must match length of ids if provided.

    Returns:
        A confirmation message indicating the number of documents updated.

    Raises:
        ValueError: If 'ids' is empty or if none of 'embeddings', 'metadatas',
                    or 'documents' are provided, or if the length of provided
                    update lists does not match the length of 'ids'.
        Exception: If the collection does not exist or if the update operation fails.
    """
    if not ids:
        raise ValueError("The 'ids' list cannot be empty.")

    if embeddings is None and metadatas is None and documents is None:
        raise ValueError(
            "At least one of 'embeddings', 'metadatas', or 'documents' "
            "must be provided for update."
        )

    # Ensure provided lists match the length of ids if they are not None
    if embeddings is not None and len(embeddings) != len(ids):
        raise ValueError(
            "Length of 'embeddings' list must match length of 'ids' list.")
    if metadatas is not None and len(metadatas) != len(ids):
        raise ValueError(
            "Length of 'metadatas' list must match length of 'ids' list.")
    if documents is not None and len(documents) != len(ids):
        raise ValueError(
            "Length of 'documents' list must match length of 'ids' list.")

    client = get_chroma_client()
    try:
        collection = client.get_collection(collection_name)
    except Exception as e:
        raise Exception(
            f"Failed to get collection '{collection_name}': {str(e)}"
        ) from e

    # Prepare arguments for update, excluding None values at the top level
    update_args = {
        "ids": ids,
        "embeddings": embeddings,
        "metadatas": metadatas,
        "documents": documents,
    }
    kwargs = {k: v for k, v in update_args.items() if v is not None}

    try:
        collection.update(**kwargs)
        return (
            f"Successfully processed update request for {len(ids)} documents in "
            f"collection '{collection_name}'. Note: Non-existent IDs are ignored by ChromaDB."
        )
    except Exception as e:
        raise Exception(
            f"Failed to update documents in collection '{collection_name}': {str(e)}"
        ) from e


@mcp.tool()
async def chroma_delete_documents(
    collection_name: str,
    ids: List[str]
) -> str:
    """Delete documents from a Chroma collection.

    Args:
        collection_name: Name of the collection to delete documents from
        ids: List of document IDs to delete

    Returns:
        A confirmation message indicating the number of documents deleted.

    Raises:
        ValueError: If 'ids' is empty
        Exception: If the collection does not exist or if the delete operation fails.
    """
    if not ids:
        raise ValueError("The 'ids' list cannot be empty.")

    client = get_chroma_client()
    try:
        collection = client.get_collection(collection_name)
    except Exception as e:
        raise Exception(
            f"Failed to get collection '{collection_name}': {str(e)}"
        ) from e

    try:
        collection.delete(ids=ids)
        return (
            f"Successfully deleted {len(ids)} documents from "
            f"collection '{collection_name}'. Note: Non-existent IDs are ignored by ChromaDB."
        )
    except Exception as e:
        raise Exception(
            f"Failed to delete documents from collection '{collection_name}': {str(e)}"
        ) from e


def validate_thought_data(input_data: Dict) -> Dict:
    """Validate thought data structure."""
    if not input_data.get("sessionId"):
        raise ValueError("Invalid sessionId: must be provided")
    if not input_data.get("thought") or not isinstance(input_data.get("thought"), str):
        raise ValueError("Invalid thought: must be a string")
    if not input_data.get("thoughtNumber") or not isinstance(input_data.get("thoughtNumber"), int):
        raise ValueError("Invalid thoughtNumber: must be a number")
    if not input_data.get("totalThoughts") or not isinstance(input_data.get("totalThoughts"), int):
        raise ValueError("Invalid totalThoughts: must be a number")
    if not isinstance(input_data.get("nextThoughtNeeded"), bool):
        raise ValueError("Invalid nextThoughtNeeded: must be a boolean")

    return {
        "sessionId": input_data.get("sessionId"),
        "thought": input_data.get("thought"),
        "thoughtNumber": input_data.get("thoughtNumber"),
        "totalThoughts": input_data.get("totalThoughts"),
        "nextThoughtNeeded": input_data.get("nextThoughtNeeded"),
        "isRevision": input_data.get("isRevision"),
        "revisesThought": input_data.get("revisesThought"),
        "branchFromThought": input_data.get("branchFromThought"),
        "branchId": input_data.get("branchId"),
        "needsMoreThoughts": input_data.get("needsMoreThoughts"),
    }


def read_file_content(file_path: str, encoding: str = "utf-8") -> str:
    """Read the content of a file as a string."""
    try:
        with open(file_path, "r", encoding=encoding) as f:
            return f.read()
    except Exception as e:
        raise Exception(f"Failed to read file '{file_path}': {str(e)}") from e


def chunk_text(text: str, chunk_size: int = 2000, overlap: int = 200) -> list[str]:
    """Chunk text into pieces of chunk_size with optional overlap."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0:
        raise ValueError("overlap must be non-negative")
    chunks = []
    start = 0
    text_length = len(text)
    while start < text_length:
        end = min(start + chunk_size, text_length)
        chunks.append(text[start:end])
        if end == text_length:
            break
        start = end - overlap  # overlap for context
    return chunks


def find_text_log_files(paths: list[str]) -> list[str]:
    """Given a list of file or directory paths, return all .txt and .log files (recursively for directories)."""
    result_files = []
    for path in paths:
        abs_path = pathlib.Path(path).expanduser().resolve()
        if abs_path.is_file() and abs_path.suffix.lower() in {'.txt', '.log'}:
            result_files.append(str(abs_path))
        elif abs_path.is_dir():
            for file in abs_path.rglob("*"):
                if file.is_file() and file.suffix.lower() in {'.txt', '.log'}:
                    result_files.append(str(file))
    return result_files


@mcp.tool()
async def chroma_add_documents_from_files(
    collection_name: str,
    file_or_dir_paths: list[str],
    chunk_size: int = 2000,
    overlap: int = 200,
    encoding: str = "utf-8",
    metadatas: list[dict] | None = None
) -> str:
    """Read content from files, directories, or archives, chunk if needed, and add as documents to a Chroma collection.
    Streams large CSVs and text files to avoid memory issues.
    Args:
        collection_name: Name of the collection to add documents to
        file_or_dir_paths: List of file, directory, or archive paths to read
        chunk_size: Max size of each text chunk (in characters)
        overlap: Overlap between text chunks (in characters)
        encoding: File encoding (default utf-8)
        metadatas: Optional list of metadata dicts (one per chunk, or None)
    Returns:
        Confirmation message with number of documents added.
    """
    import tempfile
    import itertools
    all_files = []
    temp_dirs = []
    BATCH_SIZE = 5000
    CSV_CHUNK_ROWS = 1000
    total_added = 0
    try:
        for path in file_or_dir_paths:
            abs_path = pathlib.Path(path).expanduser().resolve()
            # Handle supported archives
            if abs_path.is_file() and abs_path.suffix.lower() in {'.zip', '.tar', '.gz', '.tgz', '.rar', '.7z'}:
                if abs_path.stat().st_size > 15 * 1024 * 1024:
                    continue  # skip large archives
                tmpdir = tempfile.TemporaryDirectory()
                # keep reference to avoid premature cleanup
                temp_dirs.append(tmpdir)
                try:
                    extracted = extract_archive(str(abs_path), tmpdir.name)
                    all_files.extend(extracted)
                except Exception as e:
                    print(f"Failed to extract {abs_path}: {e}")
            elif abs_path.is_file():
                all_files.append(str(abs_path))
            elif abs_path.is_dir():
                for file in abs_path.rglob("*"):
                    if file.is_file():
                        all_files.append(str(file))
        # Now filter for .csv, .txt, .log
        vectorizable = [f for f in all_files if pathlib.Path(f).suffix.lower() in {
            '.csv', '.txt', '.log'}]
        if not vectorizable:
            raise Exception(
                "No .csv, .txt, or .log files found in provided paths or extracted archives.")
        for file_path in vectorizable:
            ext = pathlib.Path(file_path).suffix.lower()
            if ext == '.csv':
                # Stream CSV in chunks
                for chunk in pd.read_csv(file_path, chunksize=CSV_CHUNK_ROWS):
                    text_data = chunk.select_dtypes(include=['object']).astype(
                        str).agg(' '.join, axis=1).tolist()
                    if not text_data:
                        continue
                    metadatas_csv = [{"file": file_path, "row": i}
                                     for i in range(len(text_data))]
                    ids = [f"{pathlib.Path(file_path).stem}_row_{i}" for i in range(
                        len(text_data))]
                    # Batch upload
                    for i in range(0, len(text_data), BATCH_SIZE):
                        batch_docs = text_data[i:i+BATCH_SIZE]
                        batch_ids = ids[i:i+BATCH_SIZE]
                        batch_metadatas = metadatas_csv[i:i+BATCH_SIZE]
                        # Check which IDs already exist
                        client = get_chroma_client()
                        collection = client.get_or_create_collection(
                            collection_name)
                        existing_csv_ids_set: Set[str] = set(
                            collection.get(include=[])['ids'])
                        new_docs, new_ids, new_metas = [], [], []
                        update_docs, update_ids, update_metas = [], [], []
                        for doc, id, meta in zip(batch_docs, batch_ids, batch_metadatas):
                            if id in existing_csv_ids_set:
                                update_docs.append(doc)
                                update_ids.append(id)
                                update_metas.append(meta)
                            else:
                                new_docs.append(doc)
                                new_ids.append(id)
                                new_metas.append(meta)
                        if new_docs:
                            await chroma_add_documents(collection_name, new_docs, new_ids, new_metas)
                            total_added += len(new_docs)
                        if update_docs:
                            await chroma_update_documents(collection_name, update_ids, metadatas=update_metas, documents=update_docs)
            else:  # .txt or .log
                # Stream and chunk text file
                file_encoding = detect_encoding(file_path, default=encoding)
                client = get_chroma_client()
                collection = client.get_or_create_collection(collection_name)
                existing_txt_ids_set: Set[str] = set(
                    collection.get(include=[])['ids'])
                with open(file_path, 'r', encoding=file_encoding, errors='replace') as f:
                    buffer = ""
                    chunk_idx = 0
                    while True:
                        line = f.readline()
                        if not line:
                            # End of file
                            if buffer:
                                ids = [
                                    f"{pathlib.Path(file_path).stem}_chunk_{chunk_idx}"]
                                metadatas_txt = [
                                    {"file": file_path, "chunk_index": chunk_idx}]
                                if ids[0] in existing_txt_ids_set:
                                    await chroma_update_documents(collection_name, ids, metadatas=metadatas_txt, documents=[buffer])
                                else:
                                    await chroma_add_documents(collection_name, [buffer], ids, metadatas_txt)
                                    total_added += 1
                            break
                        buffer += line
                        if len(buffer) >= chunk_size:
                            ids = [
                                f"{pathlib.Path(file_path).stem}_chunk_{chunk_idx}"]
                            metadatas_txt = [
                                {"file": file_path, "chunk_index": chunk_idx}]
                            if ids[0] in existing_txt_ids_set:
                                await chroma_update_documents(collection_name, ids, metadatas=metadatas_txt, documents=[buffer])
                            else:
                                await chroma_add_documents(collection_name, [buffer], ids, metadatas_txt)
                                total_added += 1
                            if overlap > 0:
                                buffer = buffer[-overlap:]
                            else:
                                buffer = ""
                            chunk_idx += 1
        return f"Successfully added {total_added} documents to collection {collection_name} in batches."
    finally:
        for tmpdir in temp_dirs:
            tmpdir.cleanup()

# Utility: Extract supported archives if <15MB


def extract_archive(archive_path: str, extract_dir: str) -> list[str]:
    """Extract .zip, .tar, .tar.gz, .tgz, .rar, .7z archives <15MB. Returns list of extracted file paths."""
    if os.path.getsize(archive_path) > 15 * 1024 * 1024:
        raise Exception(f"Archive {archive_path} is too large (>15MB)")
    extracted_files = []
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path, 'r') as z:
            z.extractall(extract_dir)
            extracted_files = [os.path.join(extract_dir, f)
                               for f in z.namelist()]
    elif tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path, 'r:*') as t:
            t.extractall(extract_dir)
            extracted_files = [os.path.join(extract_dir, f)
                               for f in t.getnames()]
    elif archive_path.lower().endswith('.rar'):
        with rarfile.RarFile(archive_path) as r:
            r.extractall(extract_dir)
            extracted_files = [os.path.join(extract_dir, f)
                               for f in r.namelist()]
    elif archive_path.lower().endswith('.7z'):
        with py7zr.SevenZipFile(archive_path, mode='r') as z:
            z.extractall(path=extract_dir)
            extracted_files = [os.path.join(extract_dir, f)
                               for f in z.getnames()]
    else:
        raise Exception(f"Unsupported archive type: {archive_path}")
    # Flatten directories
    all_files = []
    for f in extracted_files:
        if os.path.isdir(f):
            for root, _, files in os.walk(f):
                for file in files:
                    all_files.append(os.path.join(root, file))
        else:
            all_files.append(f)
    return all_files

# Utility: Find .csv, .txt, .log files


def find_vectorizable_files(paths: list[str]) -> list[str]:
    result_files = []
    for path in paths:
        abs_path = pathlib.Path(path).expanduser().resolve()
        if abs_path.is_file() and abs_path.suffix.lower() in {'.csv', '.txt', '.log'}:
            result_files.append(str(abs_path))
        elif abs_path.is_dir():
            for file in abs_path.rglob("*"):
                if file.is_file() and file.suffix.lower() in {'.csv', '.txt', '.log'}:
                    result_files.append(str(file))
    return result_files

# Utility: Vectorize CSV file using sentence-transformers


def vectorize_csv(file_path: str, model_name: str = "all-MiniLM-L6-v2") -> tuple[list[str], list[dict]]:
    df = pd.read_csv(file_path)
    # Concatenate all string columns per row
    text_data = df.select_dtypes(include=['object']).astype(
        str).agg(' '.join, axis=1).tolist()
    if not text_data:
        return [], []
    model = SentenceTransformer(model_name)
    # This returns a list of embeddings, but for Chroma we want to add the text as documents
    # (Chroma will embed them using its own embedding function)
    # So we just return the text and metadata
    metadatas = [{"file": file_path, "row": i} for i in range(len(text_data))]
    return text_data, metadatas


def detect_encoding(file_path, default='utf-8'):
    with open(file_path, 'rb') as f:
        raw = f.read(4096)
    result = chardet.detect(raw)
    return result['encoding'] or default


def main():
    """Entry point for the Chroma MCP server."""
    parser = create_parser()
    args = parser.parse_args()

    if args.dotenv_path:
        load_dotenv(dotenv_path=args.dotenv_path)
        # re-parse args to read the updated environment variables
        parser = create_parser()
        args = parser.parse_args()

    # Validate required arguments based on client type
    if args.client_type == 'http':
        if not args.host:
            parser.error(
                "Host must be provided via --host flag or CHROMA_HOST environment variable when using HTTP client")

    elif args.client_type == 'cloud':
        if not args.tenant:
            parser.error(
                "Tenant must be provided via --tenant flag or CHROMA_TENANT environment variable when using cloud client")
        if not args.database:
            parser.error(
                "Database must be provided via --database flag or CHROMA_DATABASE environment variable when using cloud client")
        if not args.api_key:
            parser.error(
                "API key must be provided via --api-key flag or CHROMA_API_KEY environment variable when using cloud client")

    # Initialize client with parsed args
    try:
        get_chroma_client(args)
        print("Successfully initialized Chroma client")
    except Exception as e:
        print(f"Failed to initialize Chroma client: {str(e)}")
        raise

    # Initialize and run the server
    print("Starting MCP server")
    mcp.run(transport='stdio')


if __name__ == "__main__":
    main()
