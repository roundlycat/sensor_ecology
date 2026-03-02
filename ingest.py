cat > ~/semantic_archive/ingest.py << 'EOF'
import json
import os
import glob
import psycopg2
from datetime import datetime
import requests

# ── Config ────────────────────────────────────────────────────────────────────
DB_URL = "postgresql://postgres@localhost/semantic_knowledge"
OLLAMA_URL = "http://localhost:11434/api/embeddings"
OLLAMA_MODEL = "nomic-embed-text"  # 768-dim, runs well on Pi
EXPORTS_DIR = os.path.expanduser("~/semantic_archive/exports")

# ── Embedding ─────────────────────────────────────────────────────────────────
def get_embedding(text):
    """Get embedding from local Ollama."""
    r = requests.post(OLLAMA_URL, json={
        "model": OLLAMA_MODEL,
        "prompt": text[:4000]  # truncate to safe length
    })
    r.raise_for_status()
    return r.json()["embedding"]

# ── Ingest ────────────────────────────────────────────────────────────────────
def ingest_file(conn, filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        conversations = json.load(f)

    cur = conn.cursor()
    count = 0

    for conv in conversations:
        uuid = conv.get("uuid")
        title = conv.get("name", "Untitled")
        summary = conv.get("summary", "")
        created_at = conv.get("created_at")
        updated_at = conv.get("updated_at")

        # Skip if already ingested
        cur.execute("SELECT id FROM conversations WHERE source_uuid = %s", (uuid,))
        if cur.fetchone():
            continue

        # Build full text from messages
        messages = conv.get("chat_messages", [])
        full_text = "\n\n".join([
            f"[{m['sender'].upper()}] {m.get('text','')}"
            for m in messages
            if m.get("text") and m.get("sender")
        ])

        # Embed the summary (more stable semantic signal than raw messages)
        embed_text = summary if summary else title
        try:
            embedding = get_embedding(embed_text)
        except Exception as e:
            print(f"  [SKIP] Embedding failed for {title}: {e}")
            continue

        # Format embedding for pgvector
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

        cur.execute("""
            INSERT INTO conversations 
                (source_uuid, source_file, title, summary, content, 
                 created_at, updated_at, metadata, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
        """, (
            uuid,
            os.path.basename(filepath),
            title,
            summary,
            full_text,
            created_at,
            updated_at,
            json.dumps({"message_count": len(messages)}),
            embedding_str
        ))
        count += 1

        if count % 10 == 0:
            conn.commit()
            print(f"  {count} conversations ingested...")

    conn.commit()
    cur.close()
    return count

# ── Schema update ─────────────────────────────────────────────────────────────
def update_schema(conn):
    """Add source_uuid column if not present, adjust vector dim for nomic."""
    cur = conn.cursor()
    cur.execute("""
        ALTER TABLE conversations 
        ADD COLUMN IF NOT EXISTS source_uuid TEXT UNIQUE,
        ADD COLUMN IF NOT EXISTS summary TEXT,
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
    """)
    # nomic-embed-text uses 768 dims, recreate if needed
    cur.execute("""
        SELECT atttypmod FROM pg_attribute 
        WHERE attrelid = 'conversations'::regclass 
        AND attname = 'embedding';
    """)
    row = cur.fetchone()
    if row and row[0] != 768:
        print("[Schema] Adjusting embedding column to 768 dims...")
        cur.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS embedding;")
        cur.execute("ALTER TABLE conversations ADD COLUMN embedding vector(768);")
        cur.execute("DROP INDEX IF EXISTS conversations_embedding_idx;")
        cur.execute("""
            CREATE INDEX conversations_embedding_idx 
            ON conversations USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 50);
        """)
    conn.commit()
    cur.close()

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    conn = psycopg2.connect(DB_URL)
    update_schema(conn)

    json_files = glob.glob(os.path.join(EXPORTS_DIR, "**/*.json"), recursive=True)
    print(f"Found {len(json_files)} JSON files")

    total = 0
    for i, filepath in enumerate(json_files):
        print(f"[{i+1}/{len(json_files)}] {os.path.basename(filepath)}")
        try:
            n = ingest_file(conn, filepath)
            total += n
            print(f"  → {n} new conversations")
        except Exception as e:
            print(f"  [ERROR] {e}")

    conn.close()
    print(f"\nDone. Total ingested: {total}")

if __name__ == "__main__":
    main()
EOF
