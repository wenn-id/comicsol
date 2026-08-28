# Showcase evidence and contribution contract

This document defines what Comic Sol publishes as visual-quality evidence and what a
contributor must supply before another comic can join the showcase. Publication is a
narrow evidence claim, not an endorsement of a provider, model, host, or creator.

## Initial visual-quality proof

[Sunlight Courier](../samples/sunlight-courier) is retained live visual evidence and
the **only initial visual-quality sample**. It does not establish broad or universal
illustration quality.

Its claim is limited to the evidence already tracked in the repository:

- **Provider/model provenance:** [`project.json`](../samples/sunlight-courier/project.json)
  retains the capability identifier `built-in-imagegen-gpt-image-2` and its declared
  capabilities. The evidence does not separately identify a provider or model, so this
  document does not infer either one.
- **Generation attempts and provenance:** the retained
  [QA report](../samples/sunlight-courier/qa/report.md) records four attempts and no
  regenerated panels; the manifest, prompts, normalization records, and sanitized
  event log retain the available project provenance.
- **Reviewer and visual-QA evidence:** the panel QA records use the retained reviewer
  label `release-sample-reviewer`, method `agent-review`, and check-specific evidence;
  the consolidated [QA report](../samples/sunlight-courier/qa/report.md) records the
  resulting decisions.
- **Published output:** inspect [page 1](../samples/sunlight-courier/pages/page-001.png),
  [page 2](../samples/sunlight-courier/pages/page-002.png), and the
  [PDF](../samples/sunlight-courier/exports/sunlight-courier.pdf).
- **Editable evidence:** inspect the [editable project](../samples/sunlight-courier/project.json)
  and [QA report](../samples/sunlight-courier/qa/report.md).

## Known limitations

Sunlight Courier is one two-page project with four panels and one recurring character;
it is not a universal quality guarantee. The retained capability identifier is the
only available generation identity: the provider and model are not separately
identified. The retained reviewer label does not identify a human reviewer. The
available checks and report document this project only and do not prove how another
prompt, style, provider, model, host, or longer story will perform.

## Evidence boundary

Deterministic samples are mechanics-only evidence. Their local placeholder builds test
schemas, lettering, composition, export, hashing, and validation; they are never proof
of illustration quality. Green tests, path-copy tests, and generated geometry do not
promote a deterministic sample into the showcase.

## Showcase submission contract

A showcase proposal must include all of the following before review:

- explicit consent to publish the comic in the Comic Sol repository and related public
  project pages;
- confirmation that the submitter owns the work or has permission to share it;
- disclosure of the provider and model when available, without inferring missing
  provenance;
- retained generation attempts and provenance sufficient to audit the claimed result;
- retained visual-QA evidence, including honest warnings and limitations;
- removal of private source material before submission; and
- removal of credentials, tokens, account identifiers, private endpoints, raw provider
  responses, and unrelated logs.

Showcase publication consent is separate from the dogfood/report-sharing consent in
[issue #246](https://github.com/wenn-id/comicsol/issues/246). A dogfood report never
implies permission to publish the comic, story, images, or prompts. Conversely,
permission to showcase a comic does not authorize publication of unrelated report data.

Submissions that cannot establish rights, publication consent, or inspectable evidence
remain out of the showcase. Do not create replacement provenance or fill evidence gaps
with inferred provider, model, reviewer, host, date, or quality results.
