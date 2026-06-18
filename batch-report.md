# SkillSpector Batch Scan Report

**Skills scanned:** 23  
**Scanned at:** 2026-06-18 02:56:27 UTC  

## Summary

| Severity | Count |
|----------|-------|
| 🔴 CRITICAL | 5 |
| 🔴 HIGH | 3 |
| 🟡 MEDIUM | 4 |
| 🟢 LOW | 11 |

## Skills by Risk Score

| Skill | Score | Severity | Issues |
|-------|-------|----------|--------|
| `chef-assistant` | 100/100 | CRITICAL | 6 |
| `friendly-greeter` | 100/100 | CRITICAL | 5 |
| `reаd_data` | 100/100 | CRITICAL | 8 |
| `underdeclared-agent` | 100/100 | CRITICAL | 7 |
| `deploy-service` | 91/100 | CRITICAL | 4 |
| `config-reader` | 71/100 | HIGH | 3 |
| `text-summarizer` | 52/100 | HIGH | 4 |
| `markdown-formatter` | 52/100 | HIGH | 4 |
| `over-privileged-helper` | 39/100 | MEDIUM | 5 |
| `code-formatter` | 26/100 | MEDIUM | 2 |
| `file-indexer` | 26/100 | MEDIUM | 2 |
| `file-organizer` | 26/100 | MEDIUM | 2 |
| `data-processor` | 13/100 | LOW | 2 |
| `onboarding-guide` | 10/100 | LOW | 1 |
| `safe-greeting` | 0/100 | LOW | 0 |
| `terraform-deployer` | 0/100 | LOW | 0 |
| `general-assistant` | 0/100 | LOW | 0 |
| `jp-compliance-reporter` | 0/100 | LOW | 0 |
| `report-generator` | 0/100 | LOW | 0 |
| `helpful-formatter` | 0/100 | LOW | 0 |
| `creative-writing-coach` | 0/100 | LOW | 0 |
| `personal-assistant` | 0/100 | LOW | 0 |
| `code-reviewer` | 0/100 | LOW | 0 |

## 🔴 HIGH / CRITICAL Issue Details

### chef-assistant — 100/100 CRITICAL

- ** LP3**: Without declared permissions the skill's intent is opaque and cannot be validated.
  - Location: `SKILL.md:1`
  - Confidence: 70%
  - Remediation: Add a 'permissions' field to SKILL.md listing the capabilities this skill requires.

- ** E1**: Data is being sent to an external URL. This could be legitimate telemetry or data exfiltration. Manual review is recommended.
  - Location: `scripts/helper.py:21`
  - Confidence: 70%
  - Remediation: Verify the destination URL is trusted and necessary. Remove or replace with documented APIs. Ensure no secrets, tokens, or PII are transmitted.

- ** E1**: Data is being sent to an external URL. This could be legitimate telemetry or data exfiltration. Manual review is recommended.
  - Location: `scripts/helper.py:21`
  - Confidence: 80%
  - Remediation: Verify the destination URL is trusted and necessary. Remove or replace with documented APIs. Ensure no secrets, tokens, or PII are transmitted.

- ** E1**: Data is being sent to an external URL. This could be legitimate telemetry or data exfiltration. Manual review is recommended.
  - Location: `scripts/helper.py:21`
  - Confidence: 60%
  - Remediation: Verify the destination URL is trusted and necessary. Remove or replace with documented APIs. Ensure no secrets, tokens, or PII are transmitted.

- **🔴 E2**: Code accesses environment variables that may contain secrets (API keys, tokens). This is a common pattern for credential theft.
  - Location: `scripts/helper.py:15`
  - Confidence: 70%
  - Remediation: Avoid reading sensitive env vars (API keys, tokens) unless strictly required. Use secrets managers or secure config. Never log or transmit credentials.

- **🔴 P5**: This content may contain harmful instructions that could cause physical harm if followed. CRITICAL: Review carefully before use.
  - Location: `SKILL.md:38`
  - Confidence: 95%
  - Remediation: Remove all content that could lead to harmful outcomes. Add safety guardrails and human oversight for any high-risk operations.

### friendly-greeter — 100/100 CRITICAL

- **🔴 LP1**: The skill uses 'env' capability that is not listed in its permissions. This may indicate deceptive intent or missing permission declarations.
  - Location: `scripts/greet.py:1`
  - Confidence: 75%
  - Remediation: Add the 'env' permission to SKILL.md, or remove the code that requires it.

- **🔴 LP1**: The skill uses 'network' capability that is not listed in its permissions. This may indicate deceptive intent or missing permission declarations.
  - Location: `scripts/greet.py:1`
  - Confidence: 75%
  - Remediation: Add the 'network' permission to SKILL.md, or remove the code that requires it.

- ** LP4**: Declared permissions with no matching code capability may indicate removed functionality or pre-staging for future abuse.
  - Location: `SKILL.md:1`
  - Confidence: 65%
  - Remediation: Remove the 'read' permission if the corresponding capability is no longer used.

- ** E1**: Data is being sent to an external URL. This could be legitimate telemetry or data exfiltration. Manual review is recommended.
  - Location: `scripts/greet.py:12`
  - Confidence: 70%
  - Remediation: Verify the destination URL is trusted and necessary. Remove or replace with documented APIs. Ensure no secrets, tokens, or PII are transmitted.

- **🔴 E2**: Code accesses environment variables that may contain secrets (API keys, tokens). This is a common pattern for credential theft.
  - Location: `scripts/greet.py:10`
  - Confidence: 70%
  - Remediation: Avoid reading sensitive env vars (API keys, tokens) unless strictly required. Use secrets managers or secure config. Never log or transmit credentials.

### reаd_data — 100/100 CRITICAL

- ** LP4**: Declared permissions with no matching code capability may indicate removed functionality or pre-staging for future abuse.
  - Location: `SKILL.md:1`
  - Confidence: 65%
  - Remediation: Remove the 'read' permission if the corresponding capability is no longer used.

- **🔴 TP1**: HTML comments in tool metadata are invisible to users but may be processed by AI agents, enabling hidden instruction injection.
  - Location: `SKILL.md:1`
  - Confidence: 95%
  - Remediation: Remove HTML comments from metadata fields. Metadata should contain plain, visible text only.

- **🔴 TP2**: Confusable Unicode characters (e.g., Cyrillic or Greek lookalikes of Latin letters) can make a malicious tool name appear identical to a trusted one.
  - Location: `SKILL.md:1`
  - Confidence: 90%
  - Remediation: Replace all non-ASCII characters in identifier fields with their ASCII equivalents. Use a Unicode normalization/confusables check in CI.

- **🔴 TP2**: Confusable Unicode characters (e.g., Cyrillic or Greek lookalikes of Latin letters) can make a malicious tool name appear identical to a trusted one.
  - Location: `SKILL.md:1`
  - Confidence: 90%
  - Remediation: Replace all non-ASCII characters in identifier fields with their ASCII equivalents. Use a Unicode normalization/confusables check in CI.

- ** TP3**: Instruction-override phrases in parameter descriptions can hijack AI agent behavior when the tool description is processed as a prompt.
  - Location: `SKILL.md:1`
  - Confidence: 85%
  - Remediation: Remove instruction-override language from parameter descriptions. Descriptions should explain the parameter's purpose only.

- ** PE1**: Skill requests more permissions than appear necessary for its stated functionality. Review if elevated access is justified.
  - Location: `SKILL.md:5`
  - Confidence: 75%
  - Remediation: Request only the minimum permissions required. Document why each permission is needed. Remove broad permissions like '*' or 'all'.

- **🔴 P1**: This pattern attempts to override system instructions or ignore safety constraints. Without LLM analysis, manual review is recommended.
  - Location: `SKILL.md:10`
  - Confidence: 80%
  - Remediation: Remove or rewrite any text that instructs the agent to ignore prompts, override safety rules, or trust unverified content. Ensure skill content cannot be injected to alter agent behavior.

- **🔴 P1**: This pattern attempts to override system instructions or ignore safety constraints. Without LLM analysis, manual review is recommended.
  - Location: `SKILL.md:5`
  - Confidence: 90%
  - Remediation: Remove or rewrite any text that instructs the agent to ignore prompts, override safety rules, or trust unverified content. Ensure skill content cannot be injected to alter agent behavior.

### underdeclared-agent — 100/100 CRITICAL

- ** AST4**: subprocess module calls execute external commands. Without careful input validation, this enables command injection.
  - Location: `scripts/agent.py:11`
  - Confidence: 70%
  - Remediation: Use subprocess.run() with shell=False and an explicit argument list. Validate all inputs and avoid passing user-controlled data to commands.

- **🔴 TT3**: Credentials or environment variables flow to a network sink. This is a high-confidence indicator of credential exfiltration.
  - Location: `scripts/agent.py:12`
  - Confidence: 90%
  - Remediation: Never send credentials or environment variables over the network. Use secure credential stores and avoid transmitting secrets in request bodies or URLs.

- ** LP3**: Without declared permissions the skill's intent is opaque and cannot be validated.
  - Location: `SKILL.md:1`
  - Confidence: 70%
  - Remediation: Add a 'permissions' field to SKILL.md listing the capabilities this skill requires.

- ** E1**: Data is being sent to an external URL. This could be legitimate telemetry or data exfiltration. Manual review is recommended.
  - Location: `scripts/agent.py:12`
  - Confidence: 70%
  - Remediation: Verify the destination URL is trusted and necessary. Remove or replace with documented APIs. Ensure no secrets, tokens, or PII are transmitted.

- ** E1**: Data is being sent to an external URL. This could be legitimate telemetry or data exfiltration. Manual review is recommended.
  - Location: `scripts/agent.py:12`
  - Confidence: 60%
  - Remediation: Verify the destination URL is trusted and necessary. Remove or replace with documented APIs. Ensure no secrets, tokens, or PII are transmitted.

- **🔴 E2**: Code accesses environment variables that may contain secrets (API keys, tokens). This is a common pattern for credential theft.
  - Location: `scripts/agent.py:10`
  - Confidence: 70%
  - Remediation: Avoid reading sensitive env vars (API keys, tokens) unless strictly required. Use secrets managers or secure config. Never log or transmit credentials.

- **🔴 OH1**: Model output is used without validation or sanitization. Unvalidated output injected into downstream contexts (SQL, shell, HTML) enables injection attacks and arbitrary code execution.
  - Location: `scripts/agent.py:11`
  - Confidence: 95%
  - Remediation: Validate and sanitize all model output before using it in downstream contexts. Use parameterized queries for SQL, shell quoting for commands, and HTML encoding for web output.

### deploy-service — 91/100 CRITICAL

- ** AST4**: subprocess module calls execute external commands. Without careful input validation, this enables command injection.
  - Location: `deploy.py:16`
  - Confidence: 70%
  - Remediation: Use subprocess.run() with shell=False and an explicit argument list. Validate all inputs and avoid passing user-controlled data to commands.

- ** LP3**: Without declared permissions the skill's intent is opaque and cannot be validated.
  - Location: `SKILL.md:1`
  - Confidence: 70%
  - Remediation: Add a 'permissions' field to SKILL.md listing the capabilities this skill requires.

- **🔴 OH1**: Model output is used without validation or sanitization. Unvalidated output injected into downstream contexts (SQL, shell, HTML) enables injection attacks and arbitrary code execution.
  - Location: `deploy.py:16`
  - Confidence: 95%
  - Remediation: Validate and sanitize all model output before using it in downstream contexts. Use parameterized queries for SQL, shell quoting for commands, and HTML encoding for web output.

- **🔴 TM1**: Tool parameters are crafted to achieve unintended or unsafe behavior. Parameter abuse can bypass intended safety checks (e.g. shell=True, --force, dangerous glob patterns).
  - Location: `deploy.py:16`
  - Confidence: 90%
  - Remediation: Validate all tool parameters against an allowlist. Reject dangerous parameter values (shell=True, --force, -rf /) and use safe defaults.

### config-reader — 71/100 HIGH

- **🔴 LP1**: The skill uses 'file_write' capability that is not listed in its permissions. This may indicate deceptive intent or missing permission declarations.
  - Location: `config_reader.py:1`
  - Confidence: 75%
  - Remediation: Add the 'file_write' permission to SKILL.md, or remove the code that requires it.

- **🔴 LP1**: The skill uses 'shell' capability that is not listed in its permissions. This may indicate deceptive intent or missing permission declarations.
  - Location: `config_reader.py:1`
  - Confidence: 75%
  - Remediation: Add the 'shell' permission to SKILL.md, or remove the code that requires it.

- ** LP4**: Declared permissions with no matching code capability may indicate removed functionality or pre-staging for future abuse.
  - Location: `SKILL.md:1`
  - Confidence: 65%
  - Remediation: Remove the 'read:files' permission if the corresponding capability is no longer used.

### text-summarizer — 52/100 HIGH

- ** LP3**: Without declared permissions the skill's intent is opaque and cannot be validated.
  - Location: `SKILL.md:1`
  - Confidence: 70%
  - Remediation: Add a 'permissions' field to SKILL.md listing the capabilities this skill requires.

- ** E1**: Data is being sent to an external URL. This could be legitimate telemetry or data exfiltration. Manual review is recommended.
  - Location: `summarizer.py:8`
  - Confidence: 70%
  - Remediation: Verify the destination URL is trusted and necessary. Remove or replace with documented APIs. Ensure no secrets, tokens, or PII are transmitted.

- ** E1**: Data is being sent to an external URL. This could be legitimate telemetry or data exfiltration. Manual review is recommended.
  - Location: `summarizer.py:8`
  - Confidence: 80%
  - Remediation: Verify the destination URL is trusted and necessary. Remove or replace with documented APIs. Ensure no secrets, tokens, or PII are transmitted.

- ** E1**: Data is being sent to an external URL. This could be legitimate telemetry or data exfiltration. Manual review is recommended.
  - Location: `summarizer.py:9`
  - Confidence: 60%
  - Remediation: Verify the destination URL is trusted and necessary. Remove or replace with documented APIs. Ensure no secrets, tokens, or PII are transmitted.

### markdown-formatter — 52/100 HIGH

- ** AST4**: subprocess module calls execute external commands. Without careful input validation, this enables command injection.
  - Location: `formatter.py:8`
  - Confidence: 70%
  - Remediation: Use subprocess.run() with shell=False and an explicit argument list. Validate all inputs and avoid passing user-controlled data to commands.

- ** AST4**: subprocess module calls execute external commands. Without careful input validation, this enables command injection.
  - Location: `formatter.py:9`
  - Confidence: 70%
  - Remediation: Use subprocess.run() with shell=False and an explicit argument list. Validate all inputs and avoid passing user-controlled data to commands.

- ** LP3**: Without declared permissions the skill's intent is opaque and cannot be validated.
  - Location: `SKILL.md:1`
  - Confidence: 70%
  - Remediation: Add a 'permissions' field to SKILL.md listing the capabilities this skill requires.

- ** PE2**: Commands invoke sudo or root privileges. Verify this elevated access is necessary and justified.
  - Location: `formatter.py:9`
  - Confidence: 80%
  - Remediation: Avoid sudo/root unless strictly required. Prefer least-privilege patterns. If elevation is needed, document the justification and scope.



*Generated by SkillSpector v2.2.3*