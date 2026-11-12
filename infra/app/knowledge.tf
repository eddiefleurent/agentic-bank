# Policy assistant (docs/rag.md): WRITTEN, NOT APPLIED. Off unless var.knowledge_base = true, so
# merging this changes nothing on AWS. Turning it on first needs the bootstrap changes listed in
# docs/rag.md (the apply role cannot yet create S3 buckets, vector buckets or Knowledge Bases, and
# the workload boundary does not yet allow bedrock:Retrieve).
#
#   knowledge/policies/*.md --split per "## " section--> S3 docs bucket (one object per section,
#   plus a .metadata.json sidecar: doc_id, title, section, audience, effective_date)
#   --ingestion job (Bedrock, KB service role)--> Titan Text Embeddings v2 --> S3 Vectors index
#   tools Lambda --bedrock:Retrieve + audience filter set by our code--> Knowledge Base
#
# Managed by AWS: reading the objects, calling the embedding model, writing and querying vectors.
# Ours: the documents, their audience labels, the section split, the filter, the answer checks.
# The ingestion job is not a Terraform resource; start it after an apply (docs/rag.md).

variable "knowledge_base" {
  description = "Create the policy Knowledge Base and the ask_policy tool (needs the bootstrap changes in docs/rag.md)."
  type        = bool
  default     = true
}

locals {
  kb_on          = var.knowledge_base
  kb_count       = local.kb_on ? 1 : 0
  kb_embed_model = "amazon.titan-embed-text-v2:0"
  kb_embed_arn   = "arn:aws:bedrock:us-east-1::foundation-model/${local.kb_embed_model}"
  kb_dimensions  = 1024 # Titan v2; must match DIMENSIONS in src/agentic_bank/knowledge/retrievers.py
  kb_dir         = "${path.module}/../../knowledge"
  kb_docs        = jsondecode(file("${local.kb_dir}/documents.json"))

  # Split each document on "## " exactly like corpus.split_sections: element 0 is the empty text
  # before the first heading, so it is dropped. Each section becomes one object and one chunk.
  kb_parts = {
    for doc_id, meta in local.kb_docs :
    doc_id => split("\n## ", "\n${trimspace(file("${local.kb_dir}/policies/${doc_id}.md"))}")
  }
  kb_sections = merge([
    for doc_id, parts in local.kb_parts : {
      for i, part in slice(parts, 1, length(parts)) :
      format("policies/%s/%02d.md", doc_id, i + 1) => {
        doc_id         = doc_id
        title          = local.kb_docs[doc_id].title
        audience       = local.kb_docs[doc_id].audience
        effective_date = local.kb_docs[doc_id].effective_date
        section        = trimspace(split("\n", part)[0])
        body           = trimspace(substr(part, length(split("\n", part)[0]) + 1, -1))
      }
    }
  ]...)

  # What the tools Lambda gains: Retrieve on this one Knowledge Base, plus the policy answerer's
  # model and the grounding guardrail (dispute.tf). No S3, no S3 Vectors: Retrieve is served by Bedrock.
  knowledge_grants = local.kb_on ? concat(
    [{ Effect = "Allow", Action = "bedrock:Retrieve", Resource = aws_bedrockagent_knowledge_base.policies[0].arn }],
    local.policy_grants,
  ) : []
  knowledge_env = local.kb_on ? {
    KNOWLEDGE_BASE_ID = aws_bedrockagent_knowledge_base.policies[0].id
    POLICY_MODEL_ID   = local.policy_model
    GUARDRAIL_ID      = aws_bedrock_guardrail.grounding.guardrail_id
    GUARDRAIL_VERSION = aws_bedrock_guardrail_version.grounding.version
  } : {}
}

# --- Documents: synthetic, bank-owned. Public access blocked; SSE-S3 (a bank would use a CMK). ---

resource "aws_s3_bucket" "kb_docs" {
  count         = local.kb_count
  bucket        = "agentic-bank-kb-docs-${local.account}"
  force_destroy = true # synthetic documents, regenerated from the repository
}

resource "aws_s3_bucket_public_access_block" "kb_docs" {
  count                   = local.kb_count
  bucket                  = aws_s3_bucket.kb_docs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "kb_section" {
  for_each     = local.kb_on ? local.kb_sections : {}
  bucket       = aws_s3_bucket.kb_docs[0].id
  key          = each.key
  content_type = "text/markdown"
  # Must match corpus.section_text: local mode embeds this same text.
  content = format("%s (effective %s)\n\n## %s\n\n%s", each.value.title, each.value.effective_date, each.value.section, each.value.body)
}

# Bedrock reads <object key>.metadata.json beside each object; these become filterable attributes.
resource "aws_s3_object" "kb_section_metadata" {
  for_each     = local.kb_on ? local.kb_sections : {}
  bucket       = aws_s3_bucket.kb_docs[0].id
  key          = "${each.key}.metadata.json"
  content_type = "application/json"
  content = jsonencode({
    metadataAttributes = {
      doc_id         = each.value.doc_id
      title          = each.value.title
      section        = each.value.section
      audience       = each.value.audience
      effective_date = each.value.effective_date
    }
  })
}

# --- Vector store: S3 Vectors. Pay per GB stored and per query; nothing runs while idle. ---

resource "aws_s3vectors_vector_bucket" "kb" {
  count              = local.kb_count
  vector_bucket_name = "agentic-bank-kb-vectors-${local.account}"
  force_destroy      = true # vectors are rebuilt by an ingestion job
}

resource "aws_s3vectors_index" "kb" {
  count              = local.kb_count
  vector_bucket_name = aws_s3vectors_vector_bucket.kb[0].vector_bucket_name
  index_name         = "policies"
  data_type          = "float32"
  dimension          = local.kb_dimensions
  distance_metric    = "cosine"

  # Bedrock stores the chunk text and its bookkeeping as metadata; keeping them non-filterable keeps
  # them out of the 2 KB filterable-metadata limit. Our sidecar attributes (audience) stay filterable.
  metadata_configuration {
    non_filterable_metadata_keys = ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
  }
}

# --- The role Bedrock assumes to ingest and to serve Retrieve ---

data "aws_iam_policy_document" "kb_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }
    # Confused deputy: only Knowledge Bases in this account may make Bedrock act as this role.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["${format(local.arn, "bedrock")}:knowledge-base/*"]
    }
  }
}

resource "aws_iam_role" "kb" {
  count                = local.kb_count
  name                 = "agentic-bank-app-kb"
  assume_role_policy   = data.aws_iam_policy_document.kb_trust.json
  permissions_boundary = local.boundary_arn
}

resource "aws_iam_role_policy" "kb" {
  count = local.kb_count
  name  = "docs-embed-vectors"
  role  = aws_iam_role.kb[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.kb_docs[0].arn}/policies/*" },
      { Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.kb_docs[0].arn },
      { Effect = "Allow", Action = "bedrock:InvokeModel", Resource = local.kb_embed_arn },
      # This one index. QueryVectors serves Retrieve; Put/Get/DeleteVectors serve ingestion.
      {
        Effect   = "Allow"
        Action   = ["s3vectors:GetIndex", "s3vectors:QueryVectors", "s3vectors:PutVectors", "s3vectors:GetVectors", "s3vectors:DeleteVectors"]
        Resource = aws_s3vectors_index.kb[0].index_arn
      },
    ]
  })
}

# --- Knowledge Base and data source ---

resource "aws_bedrockagent_knowledge_base" "policies" {
  count    = local.kb_count
  name     = "agentic-bank-policies"
  role_arn = aws_iam_role.kb[0].arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = local.kb_embed_arn
      embedding_model_configuration {
        bedrock_embedding_model_configuration {
          dimensions          = local.kb_dimensions
          embedding_data_type = "FLOAT32"
        }
      }
    }
  }

  storage_configuration {
    type = "S3_VECTORS"
    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.kb[0].index_arn
    }
  }

  depends_on = [aws_iam_role_policy.kb]
}

resource "aws_bedrockagent_data_source" "policies" {
  count             = local.kb_count
  knowledge_base_id = aws_bedrockagent_knowledge_base.policies[0].id
  name              = "policies"

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn         = aws_s3_bucket.kb_docs[0].arn
      inclusion_prefixes = ["policies/"]
    }
  }

  # Each object is already one "## " section (split above), so Bedrock must not cut it again:
  # one section = one chunk = one citation.
  vector_ingestion_configuration {
    chunking_configuration {
      chunking_strategy = "NONE"
    }
  }
}

output "knowledge_base_id" {
  value = local.kb_on ? aws_bedrockagent_knowledge_base.policies[0].id : null
}

output "knowledge_data_source_id" {
  value = local.kb_on ? aws_bedrockagent_data_source.policies[0].data_source_id : null
}
