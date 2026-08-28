# Opt-in creator dogfood program

This guide defines the manual, creator-facing evidence route for Comic Sol. The target
program is **at least 10 external comic creators** and **20–50 valid, consented,
non-duplicate real-project reports**. Collection of those creators and reports is
deferred: the current status remains `insufficient-evidence`. Reports below either
threshold remain `insufficient-evidence`, and no adoption or success claim is allowed
until the genuine gate is met.

No creator, identity, project, report, or consent is created by this documentation.
Participation is optional.

## Who counts as an external creator

An external creator is a real person creating comics who is not being counted as a
maintainer identity, an automated agent, CI or a CI identity, a fixture, or a
duplicate/cohort alias for the same participant. Maintainer identities, automated
agents, CI identities, fixtures, deterministic samples, fabricated identities,
duplicate reports, and aliases that duplicate one creator do not count toward either
target.

A cohort alias is only a bounded anonymous label in a report. It does not prove identity
or eligibility; maintainers must verify creator/cohort eligibility separately without
publishing identity data in the aggregate.

## Creator route

Use a normal project rather than a benchmark, fixture, or project supplied to satisfy
the gate:

1. Install Comic Sol through the normal creator path in [first-run onboarding](onboarding.md).
2. Use a normal story idea of your own.
3. Use your preferred native generator, a declared external adapter, or the
   portable-handoff route, following the documented capability order.
4. Cover first project creation through a verified PDF.
5. Where naturally encountered, record handoff, blocked recovery, and resume behavior;
   do not manufacture a failure or recovery event.
6. Generate the sanitized report locally.
7. Run local preview and local validation.
8. Inspect the report before sharing it.
9. Use the dedicated GitHub issue template for the final manual submission.

### Preview, create, and validate locally

The preview prints the report without writing a submission file. Supply truthful values
for the required self-reported fields:

```bash
comic-sol dogfood preview "$PROJECT" \
  --setup-minutes SETUP_MINUTES \
  --first-project-minutes FIRST_PROJECT_MINUTES \
  --pdf-minutes PDF_MINUTES \
  --manual-intervention yes \
  --would-use-again yes
```

Use `no` instead of `yes` where that is the truthful answer. Optional current flags are
`--failed-resume-attempts N`, repeatable `--friction CATEGORY`, and
`--cohort-alias ALIAS`. From a source checkout, replace `comic-sol` with
`"$PYTHON" scripts/comic_sol.py`.

After inspection, create a consented report outside the Comic Sol project directory:

```bash
comic-sol dogfood report "$PROJECT" \
  --setup-minutes SETUP_MINUTES \
  --first-project-minutes FIRST_PROJECT_MINUTES \
  --pdf-minutes PDF_MINUTES \
  --manual-intervention yes \
  --would-use-again yes \
  --consent-to-share \
  --output "$REPORT"
comic-sol dogfood validate "$REPORT"
```

`--consent-to-share` records explicit `share_report` consent. The report field
`consent.share_report` is then `true`. Re-open the resulting JSON and inspect it before
the next step. Preview, report creation, and validation are local and perform no
submission.

### Manual submission only

Submit manually through the
[dedicated GitHub issue template](https://github.com/wenn-id/comicsol/issues/new?template=dogfood-report.yml).
There is no upload service, no telemetry, and no network submission or report collector.
The issue field accepts only the sanitized dogfood report through either:

- an attachment or stable link; or
- fenced sanitized JSON.

Do not submit any of these project or private materials:

- the Comic Sol project;
- story/source text;
- prompts or negative prompts;
- images, page PNGs, PDFs, or reference art;
- credentials, API keys, cookies, tokens, account identifiers;
- provider request/response bodies;
- endpoints containing secrets;
- filesystem paths or home directories; or
- raw logs, stack traces, exceptions, or unrelated diagnostics.

A GitHub issue is public. If a report appears to contain sensitive material, **do not
submit it** in a public GitHub issue. For ordinary questions use
[Support](../SUPPORT.md); for vulnerabilities, suspected exposure, or material that
cannot safely be public, use [private security reporting](../SECURITY.md). Never post
sensitive material publicly and move it later.

## Consent is purpose-specific

Dogfood report sharing requires explicit `share_report` consent. Dogfood
report-sharing consent covers only the sanitized report. It does not grant permission
to publish the comic, and it does not grant permission to publish story text, prompts,
images, pages, PDF, references, or project files.

Showcase publication requires a separate explicit consent and rights-to-share
confirmation under the [showcase contract](showcase.md). Consent for one purpose must
never be inferred from the other. The dogfood issue template therefore does not bundle
or request showcase-publication consent.

## Maintainer validation and aggregation

Before aggregation, maintainers must validate each candidate for:

- schema version;
- explicit consent;
- duplicate report digest;
- creator/cohort eligibility;
- bounded report contents; and
- absence of known privacy violations.

Published aggregate JSON/Markdown may occur only after maintainer validation. The
current offline command is:

```bash
"$PYTHON" scripts/dogfood_summary.py REPORT.json ... \
  --json-output dogfood-summary.json \
  --markdown-output dogfood-summary.md
```

Do not run that command on unvalidated submissions. No aggregate is published by this
work package.

Any future published aggregate documentation must label every limitation and evidence
plane independently:

- maintainer-supplied collection period;
- collection method;
- recruitment bias;
- missing data;
- host routes;
- image-generation routes;
- sample size;
- explicit numerators and denominators;
- deterministic mechanics;
- retained live visual evidence; and
- opt-in creator adoption evidence.

Deterministic mechanics, retained live visual evidence, and opt-in creator adoption
evidence are separate evidence planes. Neither fixtures nor retained samples can
replace genuine, consented external-creator participation.
