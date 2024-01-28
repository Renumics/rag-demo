#!/usr/bin/env python3
from enum import Enum
from pathlib import Path

import chromadb
import chromadb.config
import typer
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import BSHTMLLoader, DirectoryLoader
from typing_extensions import Annotated

from assistant import get_chromadb, get_embeddings_model, parse_model_name, stable_hash
from assistant.const import EMBEDDINGS_MODEL_NAME_HELP
from assistant.settings import settings

app = typer.Typer()


class OnMatchAction(str, Enum):
    IGNORE = "ignore"
    REPLACE = "replace"
    FAIL = "fail"


@app.command()
def create_db(
    docs_directory: Annotated[
        Path,
        typer.Argument(
            exists=True, file_okay=False, help="Directory with documents to index."
        ),
    ] = Path("./data/docs"),
    embeddings_model_name: Annotated[
        str, typer.Option("--embeddings", help=EMBEDDINGS_MODEL_NAME_HELP)
    ] = settings.full_embeddings_model_name,
    exist_ok: Annotated[
        bool,
        typer.Option(
            help="Do not fail if collection already exists in the vectorstore."
        ),
    ] = False,
    on_match: Annotated[
        OnMatchAction,
        typer.Option(
            case_sensitive=False,
            help="Action to perform if given documents are indexed already.",
        ),
    ] = OnMatchAction.FAIL,
) -> None:
    """
    Index documents into database.
    """
    if not exist_ok and settings.docs_db_directory.exists():
        client_settings = chromadb.config.Settings(
            is_persistent=True, persist_directory=str(settings.docs_db_directory)
        )
        client = chromadb.Client(client_settings)
        collection_names = [collection.name for collection in client.list_collections()]
        if settings.docs_db_collection in collection_names:
            raise RuntimeError(
                f"Collection '{settings.docs_db_collection}' already exists in "
                f"the vectorstore at '{settings.docs_db_directory}'. Set "
                f"'--exist-ok' for appending to existing collections."
            )
    embeddings_model = get_embeddings_model(*parse_model_name(embeddings_model_name))

    docs_vectorstore = get_chromadb(
        settings.docs_db_directory, embeddings_model, settings.docs_db_collection
    )
    indexed_doc_filepaths = sorted(
        set(
            metadata["source"]
            for metadata in docs_vectorstore.get(include=["metadatas"])["metadatas"]
        )
    )

    loader = DirectoryLoader(
        str(docs_directory),
        glob="*.html",
        loader_cls=BSHTMLLoader,
        loader_kwargs={"open_encoding": "utf-8"},
        recursive=True,
        show_progress=True,
    )
    docs = loader.load()

    if on_match == OnMatchAction.IGNORE:
        docs = [
            doc for doc in docs if doc.metadata["source"] not in indexed_doc_filepaths
        ]
    elif on_match == OnMatchAction.REPLACE:
        response = docs_vectorstore.get(include=["metadatas"])
        ids = [
            id_
            for id_, metadata in zip(response["ids"], response["metadatas"])
            if metadata["source"] in {doc.metadata["source"] for doc in docs}
        ]
        docs_vectorstore.delete(ids)
    else:
        if doc_filepaths_match := {doc.metadata["source"] for doc in docs}.intersection(
            set(indexed_doc_filepaths)
        ):
            raise RuntimeError(
                "Some of the given documents are indexed already. Set "
                "'--on-match ignore' to ignore the already indexed documents or "
                "'--on-match replace' to index them again. List of the already "
                "indexed documents: '" + "', '".join(sorted(doc_filepaths_match)) + "'."
            )

    if docs:
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000, chunk_overlap=200, add_start_index=True
        )
        splits = text_splitter.split_documents(docs)
        split_ids = list(map(stable_hash, splits))

        docs_vectorstore.add_documents(splits, ids=split_ids)
        docs_vectorstore.persist()


if __name__ == "__main__":
    app()
