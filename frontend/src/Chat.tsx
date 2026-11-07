import { useEffect, useId, useState } from "react";
import { errorMessage, request, requestId } from "./api";
import { Icon, Loading, Notice } from "./components";
import type { Message } from "./types";

export function Chat({
  templateId,
  jobId,
  documentKind,
  revision,
  onGenerated,
  onClose,
}: {
  templateId?: string;
  jobId?: string;
  documentKind?: "cv" | "cover_letter";
  revision: number;
  onGenerated?: () => void;
  onClose?: () => void;
}) {
  const messageInputId = useId();
  const [conversationId, setConversationId] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [generationQueued, setGenerationQueued] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    request<{ id: string }>("/conversations", {
      method: "POST",
      body: { template_id: templateId, job_id: jobId },
    })
      .then((value) => {
        if (active) setConversationId(value.id);
      })
      .catch((failure) => {
        if (active) {
          setError(errorMessage(failure));
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [templateId, jobId]);
  useEffect(() => {
    if (!conversationId) return;
    let active = true;
    request<{ items: Message[] }>(
      `/conversations/${encodeURIComponent(conversationId)}/messages`,
    )
      .then((value) => {
        if (active) {
          setMessages(value.items);
          setError("");
        }
      })
      .catch((failure) => {
        if (active) setError(errorMessage(failure));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [conversationId, revision, refresh]);
  async function send(event: React.FormEvent) {
    event.preventDefault();
    if (!text.trim() || sending) return;
    setSending(true);
    setError("");
    try {
      await request(
        `/conversations/${encodeURIComponent(conversationId)}/messages`,
        {
          method: "POST",
          body: { text: text.trim(), request_id: requestId() },
        },
      );
      setText("");
      setRefresh((value) => value + 1);
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setSending(false);
    }
  }
  async function regenerate() {
    if (!jobId || !documentKind || regenerating) return;
    setRegenerating(true);
    setGenerationQueued(false);
    setError("");
    try {
      await request(`/jobs/${encodeURIComponent(jobId)}/documents`, {
        method: "POST",
        body: {
          kind: documentKind,
          language: documentKind === "cv" ? "en" : "auto",
          request_id: requestId(),
        },
      });
      setGenerationQueued(true);
      onGenerated?.();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setRegenerating(false);
    }
  }
  return (
    <section className="chat-panel" aria-label="Диалог с агентом">
      <header className="chat-heading">
        <div>
          <span className="eyebrow">ВАШ ПОМОЩНИК</span>
          <h3>Обсудим детали</h3>
        </div>
        {onClose && (
          <button
            className="icon-button"
            onClick={onClose}
            aria-label="Закрыть диалог"
          >
            <Icon name="close" />
          </button>
        )}
      </header>
      <p className="muted chat-explanation">
        Расскажите о предпочтениях или уточните факты. Новые сведения нужно
        подтвердить в профиле перед созданием документов.
      </p>
      {error && <Notice>{error}</Notice>}
      <div className="messages" aria-live="polite">
        {loading ? (
          <Loading />
        ) : messages.length === 0 ? (
          <div className="chat-empty">
            <Icon name="file" size={28} />
            <p>Здесь сохранится ваш диалог.</p>
            <span>Например: «Сделай акцент на опыте API-тестирования».</span>
          </div>
        ) : (
          messages.map((message) => (
            <div
              key={message.id}
              className={`message message-${message.role === "user" ? "user" : "assistant"}`}
            >
              <span className="message-author">
                {message.role === "user" ? "Вы" : "Помощник"}
              </span>
              <p>{message.text}</p>
              {message.status && message.status !== "ready" && (
                <small>
                  {message.status === "queued"
                    ? "Ожидает ответа"
                    : message.status}
                </small>
              )}
            </div>
          ))
        )}
      </div>
      <form className="chat-form" onSubmit={send}>
        <label className="sr-only" htmlFor={messageInputId}>
          Сообщение агенту
        </label>
        <textarea
          id={messageInputId}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="Написать сообщение…"
          rows={3}
          maxLength={6000}
          disabled={!conversationId}
        />
        <div className="chat-form-bottom">
          <span>{text.length}/6000</span>
          <button
            className="button primary"
            disabled={!conversationId || !text.trim() || sending}
          >
            {sending ? "Отправляем…" : "Отправить"}
            <Icon name="send" size={16} />
          </button>
        </div>
      </form>
      {jobId && documentKind && (
        <div className="chat-regenerate">
          {generationQueued && (
            <span role="status">Повторная генерация запущена.</span>
          )}
          <button
            type="button"
            className="button secondary"
            disabled={!conversationId || regenerating}
            onClick={() => void regenerate()}
          >
            <Icon name="refresh" size={16} />
            {regenerating ? "Создаём заново…" : "Сгенерировать заново"}
          </button>
        </div>
      )}
    </section>
  );
}
