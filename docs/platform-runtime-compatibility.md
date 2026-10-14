# Windows/Linux runtime compatibility

The application was exercised on Windows 11 x64 with CPython 3.12.14 and
Ubuntu 24.04 x64/glibc 2.39 with CPython 3.12.15. This is not evidence for other
Windows versions, older Linux ABIs, ARM, or arbitrary customer GPU engines.

Use a complete CPython runtime. The application starts subprocesses with
sys.executable and Python files/modules; freezing the entry point into one
executable without adapting these subprocesses is not supported by this code.

Program code, models and native tools may be read-only. State, SQLite, outputs,
logs, document caches, Hugging Face caches and parsing scratch directories must
be on writable local storage. Create an external instance configuration with
sjfxctl.py --env-file <absolute-path> setup --data-dir <local-state>
--output-dir <local-outputs> --models-dir <resource-models>
--tools-dir <resource-tools> --parse-temp-dir <local-scratch>.
The setup command never overwrites an existing configuration. Its newly
generated configuration puts HF_HOME under the instance state cache.
Explicit resource directories are inputs and are not created or chmodded.

Configure actual resource locations with SJFX_DOCLING_ARTIFACTS_DIR,
SJFX_RAPIDOCR_MODEL_DIR, TRANSLATION_MODEL_PATH, and
TRANSLATION_CT2_MODEL_PATH. QuickMT deployments also set
TRANSLATION_QUICKMT_MODEL_DIR to the verified three-pair resource bundle.
Preserve package data and native library closures.
Layout/table weights, OCR weights, Thai/English Tesseract data and the
PyThaiNLP corpus are needed by their corresponding parsing paths.

requirements.txt and requirements-windows.txt both include the application
requirements: cryptography for device signatures and extract-msg for Outlook
MSG on both platforms. requirements-vllm.txt is for a separate Linux model
service environment. The historical requirements.lock.txt is a snapshot of
the shared Linux server environment and lacks the complete MSG dependency set;
it is not a complete new-application install entry point. Do not combine the application
requirements with a model engine's incompatible environment. If an existing
host shares its application environment with vLLM, a separate MSG-only Python
can be configured through SJFX_MSG_PYTHON. It must have its complete
extract-msg dependency set. An explicit invalid interpreter fails closed.
The interpreter's final symlink is preserved so a venv retains its identity.
The explicit parser process uses isolated Python mode and a controlled
application module path; it does not inherit PYTHONPATH/PYTHONHOME.

PST uses readpst. A private bundle may use
tools/mail-native/usr/bin/readpst and tools/mail-native/usr/lib/...
Only that child receives the private Linux library path. Incomplete native
conversions are isolated until all members pass validation; successful caches
and original files survive failures and consumer interruptions.

Generation and embedding remain independently configured. Generation uses an
administrator-managed HTTP service; vLLM/Ollama engines and Qwen or other
generation weights are not bundled. The complete offline package includes
multilingual-e5-small and an independently managed CPU embedding subprocess,
loaded only from local weights without downloads. New offline instances select
http://127.0.0.1:18083/v1 with SJFX_MANAGED_EMBEDDING=1;
SJFX_MANAGED_EMBED_PORT and SJFX_MANAGED_EMBED_MODEL_PATH control its port and
resource directory. The controller starts bundled E5 only when the selected
embedding backend, model and exact loopback endpoint match that service.
An external or disabled embedding selection skips bundled E5 on the next start;
existing selections survive upgrade. Loopback addresses refer to the application
machine. Changing vector identity still requires the existing safe index rebuild.

With bundled embedding and QA enabled, the controller owns seven roles:
protection-helper, protection-executor, embedding, web, worker, large-worker
and qa-worker. It waits for the local helper and executor, then selected E5,
then Web and workers. The helper and executor use the same managed lifecycle
on Windows and Linux. Disabling embedding or QA removes the corresponding
role. Stop and controller-failure handling reclaim only owned process trees;
external generation services retain their independent lifecycle.

Before starting a fresh instance, run sjfxctl.py --env-file <absolute-path>
init-admin. The interactive command asks for the password twice using hidden
input, stores only its hash, and never prints or logs the plaintext password.
Existing accounts reject reinitialization and remain intact during upgrade.
Formal offline installs require this initialization before Web starts, and the
first login requires changing the initial password.

Token-count caches are scoped to the actual encoder as well as its configured
identity. Request budgets use the selected model's context capacity. A smaller
context does not silently trim original evidence or claim full analysis: mail
person reading reports an instruction-capacity error before making an oversized
request, and document answers may return explicitly cited evidence extracts.
Extracts retain their own verification sources and are never reported as model
input. Normal answer generation can reduce its output reservation before dropping
a complete source; full-package synthesis keeps its separate completeness checks.

The default shared translation provider is local NLLB, preferably CPU CT2 int8;
offline_quickmt is also supported. Explicit file/package translation defaults
to the selected generation service. TRANSLATION_FILE_BACKEND may instead select
offline_nllb or offline_quickmt independently of TRANSLATION_PROVIDER; matching
offline selections reuse one provider. ENABLE_TRANSLATION=0 keeps shared/offline
translation disabled. Mail Chinese-mode translation uses its own model/review
pipeline; selecting an offline file backend does not replace that pipeline.
The audited server profile on 2026-10-14 selected QuickMT for shared and file
translation (CPU, beam 5, four threads, batch 8), with import auto-translation off.
QuickMT supports English to Chinese, Chinese to English, and Thai to Chinese
through English; document translation currently targets Chinese. Its weights
carry CC BY 4.0 notices. Optional NLLB weights carry CC-BY-NC-4.0 restrictions.

File protection inventories reject both symlinks and Windows NTFS junctions,
including a junction selected as the root. External scanning engines must return
valid completion receipts; missing YARA configuration or invalid ClamAV/Rspamd
output remains unchecked rather than certifying a clean result. The complete
offline package includes the platform ClamAV scanner and signed databases, and
its launcher manages the local desktop helper and executor on both systems.
Optional YARA/Rspamd and privileged Windows SACL/Linux auditd capabilities still
require their own explicit configuration and validation; helper startup does
not establish those capabilities.

Server throughput settings are a deployment profile, not portable hardware
capacity. Configure parsing budgets and BACKGROUND_MIN_AVAILABLE_MEMORY_MB for
the target machine. A server memory threshold can correctly leave a small
Windows machine waiting; copying that threshold is not performance acceptance.

Run doctor --scope runtime to inspect startup prerequisites, and
doctor --scope full --require-feature msg --require-feature pst for resource
and native dependency checks. Neither command proves content extraction,
translation quality or model answers. Validate actual PDF text, scanned and
table inputs; native Office/mail/archive inputs; import, exact original-text
access, retrieval, answers and exported file bytes on each target platform.

scripts/run_platform_regression.py includes deterministic QuickMT loading,
resource identity, backend selection, checkpoint/FTS publication and progress
regressions plus sjfx_protection/tests. --full runs both the application and
protection suites. Model substitutes in these suites are separate from real
native-resource and translation inference acceptance.

The repository now includes a standard-library offline bundle builder,
manifest verifier, and Windows/Linux install, upgrade, and uninstall scripts.
They package only a prepared runtime, application tree, native tools, and
offline resources, including managed CPU E5 and the local protection helper
and executor. Generation services remain independently managed and external to
the package. Resource inference and closure checks do not establish final-archive
installation acceptance. Installation/upgrade/uninstall and cold-start results
must be reported for each delivered archive separately; broader ABI
coverage and a formal authenticated customer model gateway remain separate
validation obligations. Synthetic Windows ZIP
and Linux tar.gz bundles have passed manifest verification; isolated Linux
fixtures exercise install, upgrade, migration, rollback, interruption, and
read-only uninstall. These fixtures do not establish production readiness.
Builder preflight checks the interpreter, standard library, and core startup
imports in both the input and copied runtime. Cross-target import checks are
static; parser/OCR/translation dependency closure and real inference still
require acceptance on the target platform.
