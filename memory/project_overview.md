---
name: Project Overview
description: Protenix is a security-hardened fork of ByteDance's trainable PyTorch AlphaFold 3 reproduction for biomolecular structure prediction
type: project
---

Protenix (Protein + X) is an enterprise-grade, open-source biomolecular structure prediction framework — a trainable PyTorch reproduction of AlphaFold 3.

**Why:** Virtual Hipster Corporation fork adds security hardening (official base images, Trivy/OWASP scanning, SBOM, OpenSSF Scorecard), critical bug fixes (DeepSpeed #182, Triton fallback #185, ESM loading #176), and cloud deployment support (RunPod, multi-stage Docker).

**How to apply:** Changes should maintain enterprise security posture. Docker builds are multi-variant (runtime/devel × with/without weights). CI/CD includes security scanning workflows. The active development branch is vhc-main, main is the PR target.
