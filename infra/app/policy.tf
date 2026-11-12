# Cedar, enforced by the managed policy engine outside the model: a prompt cannot talk its way past it.

resource "aws_bedrockagentcore_policy_engine" "bank" {
  name        = "agentic_bank"
  description = "Which token scope may call which bank tool"
}

locals {
  # `scope` is one space-separated string; the four patterns match the whole word, so "<read>x" fails.
  scope_condition = { for name, s in local.scopes : name => <<-CEDAR
    principal.hasTag("scope") &&
    (principal.getTag("scope") == "${s}" ||
     principal.getTag("scope") like "${s} *" ||
     principal.getTag("scope") like "* ${s}" ||
     principal.getTag("scope") like "* ${s} *")
  CEDAR
  }
  gateway_resource = "AgentCore::Gateway::\"${aws_bedrockagentcore_gateway.bank.gateway_arn}\""
}

resource "aws_bedrockagentcore_policy" "get_accounts" {
  count            = local.second_pass ? 1 : 0
  name             = "read_get_accounts"
  policy_engine_id = aws_bedrockagentcore_policy_engine.bank.policy_engine_id
  definition {
    cedar {
      statement = <<-CEDAR
        permit(
          principal is AgentCore::OAuthUser,
          action == AgentCore::Action::"bank___get_accounts",
          resource == ${local.gateway_resource}
        ) when {
          ${indent(2, trimspace(local.scope_condition.read))}
        };
      CEDAR
    }
  }
  depends_on = [aws_bedrockagentcore_gateway_target.bank]
}

resource "aws_bedrockagentcore_policy" "list_transactions" {
  count            = local.second_pass ? 1 : 0
  name             = "read_list_transactions"
  policy_engine_id = aws_bedrockagentcore_policy_engine.bank.policy_engine_id
  definition {
    cedar {
      statement = <<-CEDAR
        permit(
          principal is AgentCore::OAuthUser,
          action == AgentCore::Action::"bank___list_transactions",
          resource == ${local.gateway_resource}
        ) when {
          ${indent(2, trimspace(local.scope_condition.read))}
        };
      CEDAR
    }
  }
  # The engine rejects concurrent policy writes: one after the other.
  depends_on = [aws_bedrockagentcore_gateway_target.bank, aws_bedrockagentcore_policy.get_accounts]
}

resource "aws_bedrockagentcore_policy" "list_payees" {
  count            = local.second_pass ? 1 : 0
  name             = "read_list_payees"
  policy_engine_id = aws_bedrockagentcore_policy_engine.bank.policy_engine_id
  definition {
    cedar {
      statement = <<-CEDAR
        permit(
          principal is AgentCore::OAuthUser,
          action == AgentCore::Action::"bank___list_payees",
          resource == ${local.gateway_resource}
        ) when {
          ${indent(2, trimspace(local.scope_condition.read))}
        };
      CEDAR
    }
  }
  depends_on = [aws_bedrockagentcore_gateway_target.bank, aws_bedrockagentcore_policy.list_transactions]
}

# The amount cap is enforced twice: here, before our code runs, and again in the tool.
# amount_cents is a required integer in the schema, so it is a Cedar Long and needs no `has` guard.
resource "aws_bedrockagentcore_policy" "propose_transfer" {
  count            = local.second_pass ? 1 : 0
  name             = "propose_transfer"
  policy_engine_id = aws_bedrockagentcore_policy_engine.bank.policy_engine_id
  definition {
    cedar {
      statement = <<-CEDAR
        permit(
          principal is AgentCore::OAuthUser,
          action == AgentCore::Action::"bank___propose_transfer",
          resource == ${local.gateway_resource}
        ) when {
          ${indent(2, trimspace(local.scope_condition.propose))} &&
          context.input.amount_cents <= 100000
        };
      CEDAR
    }
  }
  depends_on = [aws_bedrockagentcore_gateway_target.bank, aws_bedrockagentcore_policy.list_payees]
}

# reading a charge or a case needs `read`; filing or answering a dispute needs `disputes`.
resource "aws_bedrockagentcore_policy" "explain_charge" {
  count            = local.second_pass ? 1 : 0
  name             = "read_explain_charge"
  policy_engine_id = aws_bedrockagentcore_policy_engine.bank.policy_engine_id
  definition {
    cedar {
      statement = <<-CEDAR
        permit(
          principal is AgentCore::OAuthUser,
          action == AgentCore::Action::"bank___explain_charge",
          resource == ${local.gateway_resource}
        ) when {
          ${indent(2, trimspace(local.scope_condition.read))}
        };
      CEDAR
    }
  }
  depends_on = [aws_bedrockagentcore_gateway_target.bank, aws_bedrockagentcore_policy.propose_transfer]
}

resource "aws_bedrockagentcore_policy" "get_dispute" {
  count            = local.second_pass ? 1 : 0
  name             = "read_get_dispute"
  policy_engine_id = aws_bedrockagentcore_policy_engine.bank.policy_engine_id
  definition {
    cedar {
      statement = <<-CEDAR
        permit(
          principal is AgentCore::OAuthUser,
          action == AgentCore::Action::"bank___get_dispute",
          resource == ${local.gateway_resource}
        ) when {
          ${indent(2, trimspace(local.scope_condition.read))}
        };
      CEDAR
    }
  }
  depends_on = [aws_bedrockagentcore_gateway_target.bank, aws_bedrockagentcore_policy.explain_charge]
}

resource "aws_bedrockagentcore_policy" "dispute_transaction" {
  count            = local.second_pass ? 1 : 0
  name             = "disputes_dispute_transaction"
  policy_engine_id = aws_bedrockagentcore_policy_engine.bank.policy_engine_id
  definition {
    cedar {
      statement = <<-CEDAR
        permit(
          principal is AgentCore::OAuthUser,
          action == AgentCore::Action::"bank___dispute_transaction",
          resource == ${local.gateway_resource}
        ) when {
          ${indent(2, trimspace(local.scope_condition.disputes))}
        };
      CEDAR
    }
  }
  depends_on = [aws_bedrockagentcore_gateway_target.bank, aws_bedrockagentcore_policy.get_dispute]
}

resource "aws_bedrockagentcore_policy" "answer_dispute" {
  count            = local.second_pass ? 1 : 0
  name             = "disputes_answer_dispute"
  policy_engine_id = aws_bedrockagentcore_policy_engine.bank.policy_engine_id
  definition {
    cedar {
      statement = <<-CEDAR
        permit(
          principal is AgentCore::OAuthUser,
          action == AgentCore::Action::"bank___answer_dispute",
          resource == ${local.gateway_resource}
        ) when {
          ${indent(2, trimspace(local.scope_condition.disputes))}
        };
      CEDAR
    }
  }
  depends_on = [aws_bedrockagentcore_gateway_target.bank, aws_bedrockagentcore_policy.dispute_transaction]
}

# knowledge.tf: reading published fees and policies is a read. The audience (customer documents
# only) is not decided here but in the tool, from the same token: Cedar sees the question, not the
# documents.
resource "aws_bedrockagentcore_policy" "ask_policy" {
  count            = local.second_pass && local.kb_on ? 1 : 0
  name             = "read_ask_policy"
  policy_engine_id = aws_bedrockagentcore_policy_engine.bank.policy_engine_id
  definition {
    cedar {
      statement = <<-CEDAR
        permit(
          principal is AgentCore::OAuthUser,
          action == AgentCore::Action::"bank___ask_policy",
          resource == ${local.gateway_resource}
        ) when {
          ${indent(2, trimspace(local.scope_condition.read))}
        };
      CEDAR
    }
  }
  depends_on = [aws_bedrockagentcore_gateway_target.bank, aws_bedrockagentcore_policy.answer_dispute]
}
