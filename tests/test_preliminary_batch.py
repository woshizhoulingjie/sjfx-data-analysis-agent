from services.preliminary_summary_batch import (
    PreliminaryBatchProtocolError,
    analyze_preliminary_batch,
    eligible_job,
)


class FakeStorage:
    def __init__(self, documents):
        self.documents = documents

    def get_document(self, scan_id, path):
        return self.documents.get(path)


class FakeLLM:
    def __init__(self, ids):
        self.ids = ids
        self.calls = 0

    def chat_json(self, system, prompt, **kwargs):
        self.calls += 1
        return {
            "json": {"documents": [
                {
                    "file_id": file_id,
                    "path": "{}.txt".format(file_id),
                    "core_summary": "{} 的中文摘要".format(file_id),
                    "conclusions": ["{} 的中文结论".format(file_id)],
                }
                for file_id in self.ids
            ]},
            "model": "fake",
            "usage": {"total_tokens": 10},
        }


def _job(job_id, path):
    return {
        "id": job_id,
        "scan_id": "scan-1",
        "owner_id": "owner-1",
        "priority": 80,
        "task_type": "generate_summary",
        "options": {
            "path": path,
            "kind": "file",
            "workflow_source": "preliminary_model_summary",
        },
    }


def test_preliminary_batch_maps_results_by_job_id_and_calls_model_once():
    from services.preliminary_summary_batch import analyze_preliminary_batch

    jobs = [_job("job-a", "a.txt"), _job("job-b", "b.txt")]
    docs = {
        "a.txt": {"text": "alpha", "evidence": [{"evidence_id": "a-1", "source_path": "a.txt", "text": "alpha 证据"}]},
        "b.txt": {"text": "beta", "evidence": [{"evidence_id": "b-1", "source_path": "b.txt", "text": "beta 证据"}]},
    }
    llm = FakeLLM(["job-a", "job-b"])
    output, ids = analyze_preliminary_batch(llm, FakeStorage(docs), jobs)
    assert llm.calls == 1
    assert ids == ["job-a", "job-b"]
    assert set(output) == {"job-a", "job-b"}
    assert output["job-a"]["summary"]["node_path"] == "a.txt"
    assert output["job-b"]["summary"]["conclusions"] == ["job-b 的中文结论"]


def test_preliminary_batch_rejects_missing_file_id():
    class MissingIdLLM(FakeLLM):
        def chat_json(self, system, prompt, **kwargs):
            return {"json": {"documents": [{"path": "a.txt", "core_summary": "摘要"}]}, "model": "fake", "usage": {}}

    jobs = [_job("job-a", "a.txt"), _job("job-b", "b.txt")]
    docs = {path: {"text": path, "evidence": []} for path in ("a.txt", "b.txt")}
    try:
        analyze_preliminary_batch(MissingIdLLM([]), FakeStorage(docs), jobs)
    except PreliminaryBatchProtocolError:
        pass
    else:
        raise AssertionError("missing file_id must reject the whole batch")


def test_only_preliminary_file_jobs_are_eligible():
    assert eligible_job(_job("job-a", "a.txt"))
    node_job = _job("job-node", "node")
    node_job["options"].update({"node_id": "node-1", "kind": "directory"})
    assert not eligible_job(node_job)
