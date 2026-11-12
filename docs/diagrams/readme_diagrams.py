"""Build the two README diagrams as self-contained SVGs (AWS icons embedded).

Run:  python3 docs/diagrams/readme_diagrams.py
Then render PNGs (what the README shows) with headless Chrome:
  google-chrome --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
    --window-size=1240,1010 --screenshot=docs/diagrams/architecture.png docs/diagrams/architecture.svg
  google-chrome --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
    --window-size=1100,940 --screenshot=docs/diagrams/tool-call-controls.png docs/diagrams/tool-call-controls.svg

Icons: AWS Architecture Icons via aws-icons-for-plantuml v23.1 (dist/*/*.png, 64 px).
Both diagrams use a solid white background so they read on GitHub light and dark themes.
"""

import base64
import os
from html import escape
from pathlib import Path

ICONS = Path(
    os.environ.get("AWS_ICONS", "~/.local/share/aws-icons-for-plantuml-v23.1/dist")
).expanduser()
OUT = Path(__file__).parent
FONT = "'Helvetica Neue', Helvetica, Arial, 'Liberation Sans', sans-serif"

INK = "#0F172A"
MUTED = "#475569"
LINE = "#64748B"


def icon(name: str) -> str:
    data = base64.b64encode((ICONS / f"{name}.png").read_bytes()).decode()
    return f"data:image/png;base64,{data}"


def text(x, y, s, size=16, weight=400, fill=INK, anchor="middle", italic=False):
    style = ' font-style="italic"' if italic else ""
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
        f'text-anchor="{anchor}"{style}>{escape(s)}</text>'
    )


def card(cx, cy, w, h, ico, title, subs, fill="#FFFFFF", stroke="#CBD5E1"):
    x, y = cx - w / 2, cy - h / 2
    out = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
    ]
    ty = y + 18
    if ico:
        out.append(f'<image href="{icon(ico)}" x="{cx - 22}" y="{y + 12}" width="44" height="44"/>')
        ty = y + 76
    out.append(text(cx, ty, title, 17, 700))
    for i, s in enumerate(subs):
        out.append(text(cx, ty + 21 + i * 19, s, 14.5, 400, MUTED))
    return "\n".join(out)


def arrow(
    d: str,
    label: str | None = None,
    lx: float = 0,
    ly: float = 0,
    color=LINE,
    dashed=False,
    anchor="middle",
):
    dash = ' stroke-dasharray="6 5"' if dashed else ""
    out = [
        f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.2"{dash} marker-end="url(#arr)"/>'
    ]
    if label:
        w = len(label) * 7.6 + 12
        bx = {"middle": lx - w / 2, "start": lx - 6, "end": lx - w + 6}[anchor]
        out.append(
            f'<rect x="{bx}" y="{ly - 15}" width="{w}" height="21" rx="4" fill="#FFFFFF" opacity="0.92"/>'
        )
        out.append(text(lx, ly, label, 14, 600, "#334155", anchor))
    return "\n".join(out)


def svg(w, h, body):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" font-family="{FONT}">
<defs>
  <marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0,0 L10,5 L0,10 z" fill="{LINE}"/>
  </marker>
  <marker id="arrRed" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0,0 L10,5 L0,10 z" fill="#B91C1C"/>
  </marker>
</defs>
<rect width="{w}" height="{h}" fill="#FFFFFF"/>
{body}
</svg>
"""


def lane(x, y, w, h, fill, stroke, label, hint):
    return "\n".join(
        [
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>',
            f'<text x="{x + 16}" y="{y + 26}" font-size="15" font-weight="700" fill="{stroke}" letter-spacing="0.8">{escape(label)}'
            f'<tspan font-weight="400" fill="{MUTED}" letter-spacing="0">  {escape(hint)}</tspan></text>',
        ]
    )


def architecture():
    W, H = 1240, 1010
    b = []
    b.append(text(30, 46, "Agentic Bank on AWS", 28, 700, INK, "start"))
    b.append(
        text(
            30,
            74,
            "Three ways in. Money and PII sit behind controls in code and IAM, not in the prompt.",
            16,
            400,
            MUTED,
            "start",
        )
    )

    # AWS account boundary
    ax, ay, aw, ah = 236, 100, 984, 790
    b.append(
        f'<rect x="{ax}" y="{ay}" width="{aw}" height="{ah}" rx="6" fill="#FFFFFF" stroke="#E7157B" stroke-width="2"/>'
    )
    b.append(
        f'<image href="{icon("Groups/AWSAccount")}" x="{ax}" y="{ay}" width="34" height="34"/>'
    )
    b.append(
        text(
            ax + 44,
            ay + 23,
            "AWS account · us-east-1 · Terraform via GitHub OIDC",
            15,
            700,
            "#BE185D",
            "start",
        )
    )

    # Top strip: identity and audit
    b.append(
        '<rect x="256" y="146" width="560" height="74" rx="12" fill="#FFFFFF" stroke="#CBD5E1" stroke-width="1.5"/>'
    )
    b.append(
        f'<image href="{icon("SecurityIdentityCompliance/Cognito")}" x="270" y="161" width="44" height="44"/>'
    )
    b.append(text(328, 177, "Cognito sign-in: password + TOTP", 17, 700, INK, "start"))
    b.append(
        text(
            328,
            200,
            "agent and app get separate OAuth clients and tokens",
            14.5,
            400,
            MUTED,
            "start",
        )
    )
    b.append(
        '<rect x="836" y="146" width="364" height="74" rx="12" fill="#FFFFFF" stroke="#CBD5E1" stroke-width="1.5"/>'
    )
    b.append(
        f'<image href="{icon("ManagementGovernance/CloudTrail")}" x="850" y="161" width="44" height="44"/>'
    )
    b.append(text(908, 177, "CloudTrail + CloudWatch Logs", 17, 700, INK, "start"))
    b.append(text(908, 200, "AWS calls and one line per tool call", 14.5, 400, MUTED, "start"))

    # Lanes
    b.append(
        lane(
            256,
            236,
            744,
            200,
            "#EFF6FF",
            "#1D4ED8",
            "AGENT CHANNEL",
            "read, explain, propose, dispute",
        )
    )
    b.append(
        lane(256, 452, 744, 196, "#FFFBEB", "#B45309", "DISPUTES", "model scores, code decides")
    )
    b.append(
        lane(
            256,
            664,
            744,
            206,
            "#F0FDF4",
            "#15803D",
            "BANK CHANNEL",
            "the customer approves, staff decide",
        )
    )

    cw, ch = 168, 140
    c1, c2, c3, c4 = 350, 535, 720, 905
    r1, r2, r3 = 350, 560, 772

    b.append(
        card(
            c1,
            r1,
            cw,
            ch,
            "ArtificialIntelligence/BedrockAgentCore",
            "AgentCore Gateway",
            ["MCP, JWT check", "Cedar ENFORCE"],
        )
    )
    b.append(card(c2, r1, cw, ch, "Compute/Lambda", "Interceptor", ["customer =", "token subject"]))
    b.append(card(c3, r1, cw, ch, "Compute/Lambda", "Tools", ["no table rights", "of its own"]))
    b.append(
        card(
            c4,
            r1,
            cw,
            ch,
            "SecurityIdentityCompliance/IdentityAccessManagementRole",
            "Per-call role",
            ["this customer's", "rows only"],
        )
    )

    b.append(
        card(
            c4,
            r2,
            cw,
            ch,
            "SecurityIdentityCompliance/KeyManagementService",
            "PII vault",
            ["Comprehend finds,", "KMS encrypts"],
        )
    )
    # dispute workflow: wide card with two icons
    y0 = r2 - 70
    b.append(
        f'<rect x="{c1 - 84}" y="{y0}" width="{c2 - c1 + 168}" height="140" rx="12" fill="#FFFFFF" stroke="#CBD5E1" stroke-width="1.5"/>'
    )
    mid = (c1 + c2) / 2
    b.append(
        f'<image href="{icon("ApplicationIntegration/StepFunctions")}" x="{mid - 54}" y="{y0 + 12}" width="44" height="44"/>'
    )
    b.append(
        f'<image href="{icon("ArtificialIntelligence/Bedrock")}" x="{mid + 10}" y="{y0 + 12}" width="44" height="44"/>'
    )
    b.append(text(mid, y0 + 76, "Dispute workflow", 17, 700))
    b.append(text(mid, y0 + 97, "Step Functions; a Bedrock model scores", 14.5, 400, MUTED))
    b.append(text(mid, y0 + 116, "the facts, code decides, only staff deny", 14.5, 400, MUTED))

    b.append(
        card(
            c1,
            r3,
            cw,
            ch,
            "NetworkingContentDelivery/APIGateway",
            "Approvals API",
            ["app tokens only,", "sign-in < 5 min"],
        )
    )
    b.append(card(c2, r3, cw, ch, "Compute/Lambda", "Approve", ["one DynamoDB", "transaction"]))
    b.append(
        card(
            c4,
            r3,
            cw,
            ch,
            "Compute/Lambda",
            "Analyst function",
            ["IAM auth URL,", "audited reveal"],
        )
    )

    # DynamoDB column
    dx, dy, dw, dh = 1020, 236, 180, 634
    b.append(
        f'<rect x="{dx}" y="{dy}" width="{dw}" height="{dh}" rx="14" fill="#FAF5FF" stroke="#7E22CE" stroke-width="1.5"/>'
    )
    b.append(
        f'<image href="{icon("Database/DynamoDB")}" x="{dx + dw / 2 - 26}" y="{dy + 220}" width="52" height="52"/>'
    )
    b.append(text(dx + dw / 2, dy + 296, "DynamoDB", 18, 700))
    for i, s in enumerate(
        [
            "accounts, payees,",
            "transfers, cases,",
            "refunds, audit",
            "",
            "partitioned by",
            "customer; PITR on",
        ]
    ):
        b.append(text(dx + dw / 2, dy + 320 + i * 19, s, 14.5, 400, MUTED))

    # Actors (outside the account)
    b.append(
        card(
            118,
            r1,
            176,
            140,
            "General/Chat",
            "Customer's agent",
            ["Claude, ChatGPT,", "any MCP client"],
            "#F1F5F9",
            "#94A3B8",
        )
    )
    b.append(
        card(
            118,
            r2,
            176,
            132,
            "General/User",
            "Customer",
            ["one person,", "two channels"],
            "#F1F5F9",
            "#94A3B8",
        )
    )
    b.append(
        card(
            118,
            r3,
            176,
            140,
            "General/Mobileclient",
            "Bank app",
            ["approve, reject,", "revoke the agent"],
            "#F1F5F9",
            "#94A3B8",
        )
    )
    b.append(
        f'<rect x="{c4 - 110}" y="914" width="220" height="70" rx="12" fill="#F1F5F9" stroke="#94A3B8" stroke-width="1.5"/>'
    )
    b.append(
        f'<image href="{icon("General/Users")}" x="{c4 - 98}" y="927" width="44" height="44"/>'
    )
    b.append(text(c4 - 42, 943, "Dispute analyst", 17, 700, INK, "start"))
    b.append(text(c4 - 42, 965, "bank staff, IAM role", 14.5, 400, MUTED, "start"))

    # Arrows
    b.append(arrow(f"M118,{r2 - 60} L118,{r1 + 72}", "asks", 146, r2 - 76, anchor="start"))
    b.append(arrow(f"M118,{r2 + 60} L118,{r3 - 72}", "approves", 146, r2 + 92, anchor="start"))
    b.append(arrow(f"M206,{r1} L{c1 - 86},{r1}", "MCP", 236, r1 - 8))
    b.append(arrow(f"M206,{r3} L{c1 - 86},{r3}", "HTTPS", 236, r3 - 8))
    b.append(arrow(f"M{c1 + 84},{r1} L{c2 - 86},{r1}"))
    b.append(arrow(f"M{c2 + 84},{r1} L{c3 - 86},{r1}"))
    b.append(arrow(f"M{c3 + 84},{r1} L{c4 - 86},{r1}"))
    b.append(arrow(f"M{c4 + 84},{r1} L{dx - 2},{r1}"))
    b.append(
        arrow(
            f"M{c3 + 50},{r1 + 70} L{c4 - 40},{r2 - 72}",
            "tokenize PII",
            c4 - 52,
            (r1 + r2) / 2 + 2,
            anchor="start",
        )
    )
    b.append(
        arrow(
            f"M{c3 - 20},{r1 + 70} L{c3 - 20},{r2 - 20} L{c2 + 86},{r2 - 20}",
            "start",
            c3 - 12,
            (r1 + r2) / 2 + 2,
            anchor="start",
        )
    )
    b.append(
        arrow(
            f"M{c2 + 40},{r2 + 70} L{c2 + 40},{r2 + 78} L{dx - 2},{r2 + 78}",
            "refund",
            684,
            r2 + 73,
            anchor="start",
        )
    )
    b.append(arrow(f"M{c1 + 84},{r3} L{c2 - 86},{r3}"))
    b.append(
        arrow(
            f"M{c2},{r3 + 70} L{c2},{r3 + 84} L{dx - 2},{r3 + 84}",
            "execute once",
            700,
            r3 + 78,
            anchor="start",
        )
    )
    b.append(arrow(f"M{c4},914 L{c4},{r3 + 72}", "decide", c4 + 10, 902, anchor="start"))
    b.append(
        arrow(
            f"M{c4 - 84},{r3 - 30} L{c3 - 50},{r3 - 30} L{c3 - 50},{r2 + 30} L{c2 + 86},{r2 + 30}",
            "resume",
            c3 - 40,
            r3 - 38,
            anchor="start",
        )
    )
    b.append(
        arrow(
            f"M{c4},{r3 - 70} L{c4},{r2 + 72}", "reveal", c4 + 10, (r2 + r3) / 2 + 4, anchor="start"
        )
    )

    return svg(W, H, "\n".join(b))


def flow():
    W, H = 1100, 940
    b = []
    b.append(text(30, 46, "One tool call, five checks", 28, 700, INK, "start"))
    b.append(
        text(
            30,
            74,
            "A hijacked agent tries to send $5,000 from another customer's account. Each layer stops a different failure.",
            16,
            400,
            MUTED,
            "start",
        )
    )

    # request bubble
    b.append(
        '<rect x="30" y="98" width="1040" height="78" rx="14" fill="#F1F5F9" stroke="#94A3B8" stroke-width="1.5"/>'
    )
    b.append(f'<image href="{icon("General/Chat")}" x="46" y="115" width="44" height="44"/>')
    b.append(text(106, 130, "Agent → MCP tools/call  propose_transfer", 17, 700, INK, "start"))
    b.append(
        '<text x="106" y="156" font-size="15.5" fill="#334155" font-family="DejaVu Sans Mono, Menlo, monospace">'
        '{"customer": "sam", "amount_cents": 500000, "payee_id": "pay_x"}  + Bearer token</text>'
    )

    steps = [
        (
            "ArtificialIntelligence/BedrockAgentCore",
            "1  Gateway JWT authorizer",
            "signature, issuer, expiry, client",
            "Missing, forged, expired or wrong-client token",
            "401",
        ),
        (
            "Compute/Lambda",
            "2  Request interceptor",
            "customer := the token's subject",
            '"customer": "sam" is overwritten; a revoked agent is refused',
            "refused",
        ),
        (
            "ArtificialIntelligence/BedrockAgentCore",
            "3  Cedar policy, ENFORCE, default deny",
            "tool needs its scope; amount ≤ $1,000",
            "Missing scope, or $5,000 > $1,000",
            "denied",
        ),
        (
            "SecurityIdentityCompliance/IdentityAccessManagementRole",
            "4  Per-call IAM session policy",
            "dynamodb:LeadingKeys = subject",
            "A bug or injected ID reading another customer's rows",
            "AccessDenied",
        ),
        (
            "Database/DynamoDB",
            "5  Propose, never execute",
            "agent-side role writes proposals only",
            "Money moves only when the customer approves in the app",
            "PENDING",
        ),
    ]
    y = 214
    for ico, title, how, stops, verdict in steps:
        h = 104
        b.append(
            f'<rect x="30" y="{y}" width="600" height="{h}" rx="12" fill="#FFFFFF" stroke="#CBD5E1" stroke-width="1.5"/>'
        )
        b.append(f'<image href="{icon(ico)}" x="48" y="{y + 26}" width="52" height="52"/>')
        b.append(text(118, y + 44, title, 19, 700, INK, "start"))
        b.append(text(118, y + 72, how, 16, 400, MUTED, "start"))
        # stop box
        b.append(
            f'<rect x="690" y="{y + 6}" width="380" height="{h - 12}" rx="12" fill="#FEF2F2" stroke="#FCA5A5" stroke-width="1.5"/>'
        )
        b.append(text(708, y + 32, "STOPS", 13, 700, "#B91C1C", "start"))
        b.append(
            f'<rect x="{1052 - len(verdict) * 9.4 - 16}" y="{y + 16}" width="{len(verdict) * 9.4 + 16}" height="24" rx="12" fill="#B91C1C"/>'
        )
        b.append(text(1052 - (len(verdict) * 9.4 + 16) / 2, y + 33, verdict, 14, 700, "#FFFFFF"))
        # wrap the stops line
        words, lines, cur = stops.split(), [], ""
        for w_ in words:
            if len(cur) + len(w_) + 1 > 42:
                lines.append(cur)
                cur = w_
            else:
                cur = (cur + " " + w_).strip()
        lines.append(cur)
        for i, ln in enumerate(lines):
            b.append(text(708, y + 60 + i * 20, ln, 15.5, 400, "#7F1D1D", "start"))
        b.append(
            f'<path d="M630,{y + h / 2} L686,{y + h / 2}" stroke="#B91C1C" stroke-width="2" stroke-dasharray="5 4" fill="none" marker-end="url(#arrRed)"/>'
        )
        if title[0] != "5":
            b.append(arrow(f"M330,{y + h} L330,{y + h + 22}"))
        y += h + 22

    b.append(arrow(f"M330,{y - 22} L330,{y + 8}"))
    b.append(
        f'<rect x="30" y="{y + 10}" width="1040" height="70" rx="14" fill="#F0FDF4" stroke="#15803D" stroke-width="1.5"/>'
    )
    b.append(
        f'<image href="{icon("General/Mobileclient")}" x="46" y="{y + 23}" width="44" height="44"/>'
    )
    b.append(text(106, y + 40, "Customer approves in the bank app", 18, 700, "#14532D", "start"))
    b.append(
        text(
            106,
            y + 64,
            "fresh password + TOTP, hash of what they saw, one DynamoDB transaction: exactly once",
            15.5,
            400,
            "#166534",
            "start",
        )
    )
    return svg(W, H, "\n".join(b))


if __name__ == "__main__":
    (OUT / "architecture.svg").write_text(architecture())
    (OUT / "tool-call-controls.svg").write_text(flow())
    print("wrote architecture.svg, tool-call-controls.svg")
