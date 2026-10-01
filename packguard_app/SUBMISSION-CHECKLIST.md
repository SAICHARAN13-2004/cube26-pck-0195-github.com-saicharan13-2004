# PackGuard Submission Checklist

## Repository

- [x] Working outbound Pack Manager workflow
- [x] `README.md`
- [x] `ARCHITECTURE.md`
- [x] `EVALUATION_REPORT.md`
- [x] `DEMO-SCRIPT.md`
- [x] Bounded Pack agent handoff with auditable action selection
- [ ] Push the final commits to the registered GitHub fork
- [ ] Confirm all Round 2 commits are inside the authorized build window
- [ ] Confirm no secrets, local databases, uploads, or tokens are committed

## Evaluation

- [x] Deterministic SKU and quantity tests
- [x] Missing and extra item behavior
- [x] `PASS` cannot mask uncertain pack evidence
- [x] CUBE contract serializer/API focused tests passed (8 tests, as reported)
- [x] Full local test suite passed (80 passed)
- [ ] Capture and independently label 50 unseen outbound pack images
- [ ] Run the held-out vision evaluation
- [ ] Fill in precision, recall, false positives, false negatives, uncertain rate, and false-SEAL rate
- [x] Keep automatic sealing disabled until calibration passes

The held-out folder currently has 50 images and annotator manifests, but reviewer independence must be confirmed. The saved prediction run timed out on all 50 images, so it produced no usable vision metrics. Do not present timeouts as model accuracy.

## Demo

The user reports that a demo video has been recorded locally; it has not yet been uploaded or reviewed here.

- [ ] Record the correct-pack scenario
- [ ] Record a wrong or extra-item scenario
- [ ] Record an uncertain or recapture scenario
- [ ] Show the evidence record and audit trail
- [ ] Upload the video and paste its URL into the buildathon submission

## External submission

- [ ] Deploy behind HTTPS if a public URL is required
- [ ] Verify the deployment URL in a clean browser
- [ ] Add the deployment URL to the submission form
- [ ] Add the mandatory LinkedIn post URL
- [ ] Submit before the published deadline; keep the final submission immutable
