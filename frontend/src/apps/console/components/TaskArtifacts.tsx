/** 展示 A2A 已登记的产物，预览和下载使用任务结束时的快照。 */
import { useEffect, useState } from "react";
import { api } from "../../../api/os";
import type { TaskArtifact, TaskDetails } from "../../../types";

/** 从活动总览按任务读取结果，切换任务时取消旧查询。 */
export default function TaskArtifacts({ taskId }: { taskId: string }) {
  const [task, setTask] = useState<TaskDetails>();
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [downloading, setDownloading] = useState<string>();

  useEffect(() => {
    const request = new AbortController();
    api.getTask(taskId, request.signal).then(setTask, () => {
      if (!request.signal.aborted) setError("任务详情加载失败");
    });
    return () => request.abort();
  }, [taskId, revision]);

  async function download(artifact: TaskArtifact) {
    setDownloading(artifact.artifactId);
    setError("");
    try {
      const blob = await api.downloadTaskArtifact(taskId, artifact.artifactId);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = artifact.name || "artifact.txt";
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setError("产物下载失败，请重试");
    } finally {
      setDownloading(undefined);
    }
  }

  return (
    <>
      <button type="button" className="btn-sm btn-secondary" onClick={() => {
        setError("");
        setRevision(current => current + 1);
      }}>刷新任务</button>
      {error && <p role="alert">{error}</p>}
      {!task && !error && <p role="status">正在加载任务</p>}
      {task && (
        <>
          <p>任务状态 {task.status}</p>
          {task.error && <p role="alert">{task.error}</p>}
          {task.artifacts.length === 0 && <p>暂无已登记产物</p>}
          {task.artifacts.map(artifact => {
            const texts = artifact.parts
              .filter(part => part.kind === "text" && typeof part.text === "string")
              .map(part => part.text);
            return (
              <details key={artifact.artifactId} style={{ marginBottom: 12 }}>
                <summary>{artifact.name}</summary>
                {texts.length > 0 && (
                  <>
                    <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{texts.join("\n")}</pre>
                    <button type="button" className="btn-sm btn-secondary"
                      onClick={() => download(artifact)} disabled={downloading !== undefined}>
                      {downloading === artifact.artifactId ? "正在下载" : "下载"}
                    </button>
                  </>
                )}
              </details>
            );
          })}
        </>
      )}
    </>
  );
}
