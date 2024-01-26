import os
from pathlib import Path
import sys
from typing import Any, Callable, Dict, List, Literal, Optional, Union, get_args
import hashlib
import json

import dotenv
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableParallel, RunnablePassthrough
from langchain_openai import (
    ChatOpenAI,
    OpenAIEmbeddings,
    AzureChatOpenAI,
    AzureOpenAIEmbeddings,
)
from langchain_core.vectorstores import VectorStore, VectorStoreRetriever
from langchain.vectorstores.chroma import Chroma
from langchain_community.llms import HuggingFacePipeline 
from transformers import AutoModel 
from transformers import LlamaTokenizer, LlamaForCausalLM, GenerationConfig, pipeline, BitsAndBytesConfig , CodeGenTokenizer , AutoModelForCausalLM, 
from transformers import AutoTokenizer 
import torch 

dotenv.load_dotenv(override=True)

if sys.platform == "linux":
    # For Linux, `sqlite3` can be outdated, so use the installed Python package.
    __import__("pysqlite3")
    sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")


ModelType = Literal["openai", "azure", "hf"]
PredefinedRelevanceScoreFn = Literal["l2", "ip", "cosine"]
RelevanceScoreFn = Union[PredefinedRelevanceScoreFn, Callable[[float], float]]
RetrieverSearchType = Literal["similarity", "similarity_score_threshold", "mmr"]


MODEL_TYPES: Dict[ModelType, str] = {
    "openai": "OpenAI",
    "azure": "Azure OpenAI",
    "hf": "HuggingFace",
}
PREDEFINED_RELEVANCE_SCORE_FNS: Dict[PredefinedRelevanceScoreFn, str] = {
    "l2": "Squared euclidean distance",
    "ip": "Inner product",
    "cosine": "Cosine similarity",
}
RETRIEVER_SEARCH_TYPES: Dict[RetrieverSearchType, str] = {
    "similarity": "Similarity",
    "similarity_score_threshold": "Similarity with score threshold",
    "mmr": "Maximal marginal relevance (MMR)",
}


def get_embeddings_model(
    name: str, model_type: Optional[ModelType] = None
) -> Embeddings:
    if model_type is None:
        if os.getenv("OPENAI_API_TYPE") == "azure":
            model_type = "azure"
        elif "OPENAI_API_KEY" in os.environ:
            model_type = "openai"
        else:
            model_type = "hf"
    if model_type == "azure":
        return AzureOpenAIEmbeddings(azure_deployment=name)
    if model_type == "openai":
        return OpenAIEmbeddings(model=name)
    if model_type == "hf":
        encode_kwargs = {"normalize_embeddings": True}
        mode_kwargs = {"device": "cuda"}
        return HuggingFaceEmbeddings(
            model_name=name, encode_kwargs=encode_kwargs, model_kwargs=mode_kwargs
        )
    raise TypeError("Unknown model type.")


def get_chat_model(name: str, model_type: Optional[ModelType] = None) -> BaseChatModel:
    if model_type is None:
        if os.getenv("OPENAI_API_TYPE") == "azure":
            model_type = "azure"
        elif "OPENAI_API_KEY" in os.environ:
            model_type = "openai"
        else:
            model_type = "hf"
    if model_type == "azure":
        return AzureChatOpenAI(azure_deployment=name, temperature=0.0)
    if model_type == "openai":
        return ChatOpenAI(model=name, temperature=0.0)
    if model_type == "hf":
        tokenizer = AutoTokenizer.from_pretrained(name)
        quantization_config = BitsAndBytesConfig(llm_int8_enable_fp32_cpu_offload=True)
        base_model =  AutoModelForCausalLM.from_pretrained( # AutoModelForCausalLM.from_pretrained( ##AutoModel
            name,
            load_in_8bit=True,
            torch_dtype=torch.float16, 
            device_map='auto',
            quantization_config=quantization_config
        )
        pipe = pipeline(
            "text-generation",
            model=base_model,
            tokenizer=tokenizer,
            max_length=256,
            temperature=0.0,
            top_p=0.95,
            repetition_penalty=1.2
        )
        local_llm = HuggingFacePipeline(pipeline=pipe)
        return local_llm

        raise NotImplementedError("HuggingFace chat models are not implemented.")
    raise TypeError("Unknown model type.")


def get_chromadb(
    persist_directory: Path,
    embeddings_model: Embeddings,
    collection_name: str,
    relevance_score_fn: Optional[RelevanceScoreFn] = None,
) -> Chroma:
    """
    https://docs.trychroma.com/usage-guide#changing-the-distance-function
    """
    if relevance_score_fn is None:
        return Chroma(
            collection_name=collection_name,
            embedding_function=embeddings_model,
            persist_directory=str(persist_directory),
        )
    if isinstance(relevance_score_fn, str):
        assert relevance_score_fn in get_args(PredefinedRelevanceScoreFn)
        return Chroma(
            collection_name=collection_name,
            embedding_function=embeddings_model,
            persist_directory=str(persist_directory),
            collection_metadata={"hnsw:space": relevance_score_fn},
        )
    return Chroma(
        collection_name=collection_name,
        embedding_function=embeddings_model,
        persist_directory=str(persist_directory),
        relevance_score_fn=relevance_score_fn,
    )


def get_retriever(
    vectorstore: VectorStore,
    k: Optional[int] = None,
    search_type: RetrieverSearchType = "similarity",
    score_threshold: Optional[float] = None,
    fetch_k: Optional[int] = None,
    lambda_mult: Optional[float] = None,
) -> VectorStoreRetriever:
    kwargs: Dict[str, Any] = {"search_type": search_type}
    search_kwargs: Dict[str, Any] = {}
    if k is not None:
        search_kwargs["k"] = k
    if search_type == "similarity_score_threshold":
        assert score_threshold is not None
        search_kwargs["score_threshold"] = score_threshold
    elif search_type == "mmr":
        if fetch_k is not None:
            search_kwargs["fetch_k"] = fetch_k
        if lambda_mult is not None:
            search_kwargs["lambda_mult"] = lambda_mult
    kwargs["search_kwargs"] = search_kwargs
    return vectorstore.as_retriever(**kwargs)


def format_doc(doc: Document) -> str:
    return f"Content: {doc.page_content}\nSource: {doc.metadata['source']}"


def format_docs(docs: List[Document]) -> str:
    return "\n\n".join(
        f"Content: {doc.page_content}\nSource: {doc.metadata['source']}" for doc in docs
    )


def get_rag_chain(
    retriever: VectorStoreRetriever, chat_model: BaseChatModel
) -> Runnable:
    template = """You are an assistant for question-answering tasks on Formula One (F1) documentation.
Given the following extracted parts of a long document and a question, create a final answer with used references (named "sources").
Keep the answer concise. If you don't know the answer, just say that you don't know. Don't try to make up an answer.
ALWAYS return used sources in your answer.

QUESTION: {question}
=========
{source_documents}
=========
FINAL ANSWER: """
    prompt = ChatPromptTemplate.from_template(template)
    rag_chain_from_docs = (
        RunnablePassthrough.assign(
            source_documents=(lambda x: format_docs(x["source_documents"]))
        )
        | prompt
        | chat_model
        | StrOutputParser()
    )
    rag_chain_with_source = RunnableParallel(
        {  # type: ignore
            "source_documents": retriever,
            "question": RunnablePassthrough(),
        }
    ).assign(answer=rag_chain_from_docs)
    return rag_chain_with_source


def stable_hash(doc: Document) -> str:
    """
    Stable hash document based on its metadata.
    """
    return hashlib.sha1(json.dumps(doc.metadata, sort_keys=True).encode()).hexdigest()
