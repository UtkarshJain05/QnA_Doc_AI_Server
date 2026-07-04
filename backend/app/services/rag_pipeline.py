import time
import logging 
from supabase import create_client

from app.config import (
    SUPABASE_URL,
    SUPABASE_KEY,
    HUGGINGFACEHUB_API_TOKEN,
    GROQ_API_KEY,
)

from langchain_groq import ChatGroq
from langchain_huggingface import HuggingFaceEndpointEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

# -------------------------
# Setup Clients
# -------------------------

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

embeddings = HuggingFaceEndpointEmbeddings(
    model="sentence-transformers/all-MiniLM-L6-v2",
    huggingfacehub_api_token=HUGGINGFACEHUB_API_TOKEN,
)

llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0,
    api_key = GROQ_API_KEY
)


# -------------------------
# Ingestion
# -------------------------

def ingest_pdf_text(raw_text: str, session_id: str, filename: str, file_path: str):
    """
    Splits text, creates embeddings, and stores them in Supabase.
    """

    if len(raw_text.strip()) < 80:
        raise Exception(
            "The document contains very little readable text. It may be image-heavy or stylized."
        )
    
    # Step 1: Split text into chunks
    splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=150)
    chunks = splitter.split_text(raw_text)
    print(f"✅ [DONE] Created {len(chunks)} chunks.")
    
    MAX_CHUNKS = 1500
    if len(chunks) > MAX_CHUNKS:
        raise Exception("Document too large. Please upload a smaller PDF.")

    # Step 2: Generate embeddings
    embed_start = time.time()
    BATCH_SIZE = 100
    vectors = []
    
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        batch_vectors = embeddings.embed_documents(batch)
        vectors.extend(batch_vectors)
    
    embed_elapsed = round(time.time() - embed_start, 2)
    print(f"✅ [DONE] Embeddings generated for {len(chunks)} chunks in {embed_elapsed}s.")

    # Step 3: Prepare records for Supabase
    db_start = time.time()

    data = []
    for text, vector in zip(chunks, vectors):
        data.append(
            {
                "session_id": session_id,
                "content": text,
                "embedding": vector,
                "metadata": {
                    "source": filename,
                    "file_path": file_path,
                },
            }
        )

    # Step 4: Insert into Supabase
    INSERT_BATCH_SIZE = 300
    for i in range(0, len(data), INSERT_BATCH_SIZE):
        batch = data[i : i + INSERT_BATCH_SIZE]
        response = supabase.table("documents").insert(batch).execute()

    if response.data is None:
        raise Exception("Failed to insert embeddings into database.")
    
    db_elapsed = round(time.time() - db_start, 2)
    print(f"✅ [DONE] Supabase insertion complete in {db_elapsed}s.")
    
    return len(chunks)


# -------------------------
# Retrieval
# -------------------------

def retrieve_similar(query: str, session_id: str, limit: int = 5):

    query_vector = embeddings.embed_query(query)

    response = supabase.rpc(
        "match_documents",
        {
            "query_embedding": query_vector,
            "match_count": limit,
            "filter_session": session_id,
        },
    ).execute()

    return response.data or []


# -------------------------
# Question Answering
# -------------------------

def ask_question_stream(query: str, session_id: str):
    query_lower = query.lower().strip()

    if query_lower in ["hi", "hello", "hey"]:
        yield "Hello! I'm QnA Bot. Ask me anything about your Doc."
        return
    
    matches = retrieve_similar(query, session_id)

    if not matches:
        yield "The information is not available in the uploaded document."
        return

    # use matches directly (no strict filtering)
    formatted_excerpts = [
        f"--- Excerpt {i+1} ---\n{doc['content']}" for i, doc in enumerate(matches)
    ]
    context = "\n\n".join(formatted_excerpts)
    
    final_prompt = f"""
You are QnA Bot, a friendly AI assistant that helps users understand information inside an uploaded PDF document.

Behavior Guidelines:

1. Answer questions using ONLY the information present in the provided context.
2. Do NOT use outside knowledge, assumptions, or general world knowledge.
3. You are allowed to infer connections between names and events if they appear reasonably close to each other in the text.
4. If a question contains multiple parts, answer only the part that is supported by the document and ignore unrelated parts.
5. If the answer cannot be found in the context, politely respond with:
"The information is not available in the uploaded document."

Tone Guidelines:

• Be friendly and helpful.
• Write in clear natural language.
• Keep answers concise and easy to understand.
• Do not mention the rules or say you are restricted.

Greeting Rule:
If the user greets you or asks about you, reply briefly like:
"Hello! I'm QnA Bot. Upload a document and I'll help you explore the information inside it."

Context:
{context}

User Question:
{query}
"""

    try:
        for chunk in llm.stream(final_prompt):
            if chunk.content:
                yield chunk.content

    except Exception as e:
        logging.error(f"Error querying llm API endpoint: {e}")
        yield "⚠️ The AI service is temporarily busy. Please try again in a few moments."


# -------------------------
# Delete Session
# -------------------------

def delete_session_data(session_id: str):
    supabase.table("documents").delete().eq("session_id", session_id).execute()