# tenant-support-agent
An agentic RAG assistant for the tenant portal that securely retrieves property documents, answers lease and payment questions, and supports tenant requests.


### Repository Structure
```md
backend/src/
├── agents/
├── api/
├── auth/
├── ingestion/
├── retrieval/
├── tools/
├── evaluation/
└── models/

frontend/
evals/
docs/
data/synthetic/
```


S3 (docs) → Lambda/ECS (parse + chunk + embed via Bedrock) → RDS pgvector (store)
Query → RDS pgvector (hybrid search) → reranker (SageMaker or in-code) → Bedrock LLM → response