# Security

Agentic Bank is a demo: a fictional bank with synthetic data. It is a reference and learning
architecture, not a production system, and it holds no real customers, money or personal data.
Its known gaps are listed in [docs/architecture.md](docs/architecture.md#known-limits-lean-build).

## Reporting a vulnerability

If you find a flaw in the code or the Terraform (for example a way around one of the controls in
the risk table), please report it privately through GitHub: open the repository's **Security** tab
and choose **Report a vulnerability** (a private security advisory). Please do not open a public
issue for it.

This is a personal project with no support commitment or response time. Reports are read and
fixes are made on a best-effort basis.

## If you deploy it

You run it in your own AWS account, at your own cost and risk. Use synthetic data only. Never commit
Terraform state, `terraform.tfvars`, `backend.hcl` or credentials; the `.gitignore` covers them.
