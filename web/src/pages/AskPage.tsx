import { type FormEvent, useEffect, useReducer, useRef, useState } from "react";

import { ApiError } from "../api/client";
import { AnswerView } from "../components/AnswerView";
import { Alert, Button, Card, PageHeader, inputClass } from "../components/ui";
import { type AnswerEvent, type AnswerState, answerReducer, initialAnswer } from "../lib/answer";
import { streamAnswer } from "../lib/askStream";

type Action = AnswerEvent | { event: "reset" };

function reducer(state: AnswerState | null, action: Action): AnswerState | null {
  if (action.event === "reset") return initialAnswer;
  return state ? answerReducer(state, action) : state;
}

export function AskPage() {
  const [question, setQuestion] = useState("");
  const [asked, setAsked] = useState("");
  const [answer, dispatch] = useReducer(reducer, null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => () => abort.current?.abort(), []); // stop streaming when leaving the page

  const busy = answer?.status === "streaming";

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const q = question.trim();
    if (!q || busy) return;
    abort.current?.abort();
    abort.current = new AbortController();
    setError(null);
    setAsked(q);
    dispatch({ event: "reset" });
    try {
      await streamAnswer(q, dispatch, abort.current.signal);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      setError(e instanceof Error ? e : new Error(String(e)));
      dispatch({ event: "error", data: { message: "" } });
    }
  }

  const questionError = error instanceof ApiError ? error.messageFor("question") : undefined;

  return (
    <>
      <PageHeader
        title="Ask the knowledge base"
        description="Answers use only the documents you are allowed to read, and cite the passages they rely on."
      />
      <Card>
        <form onSubmit={onSubmit} className="flex flex-col gap-3 sm:flex-row">
          <label htmlFor="question" className="sr-only">
            Question
          </label>
          <input
            id="question"
            className={inputClass}
            placeholder="e.g. How many days can I work from home?"
            value={question}
            maxLength={2000}
            onChange={(e) => setQuestion(e.target.value)}
            aria-invalid={questionError ? true : undefined}
            autoFocus
          />
          <Button type="submit" disabled={busy || !question.trim()}>
            {busy ? "Answering…" : "Ask"}
          </Button>
        </form>
        {error && (
          <div className="mt-4">
            <Alert tone="error">
              {questionError ?? error.message}
              {error instanceof ApiError && error.retryAfterSeconds
                ? ` Try again in ${error.retryAfterSeconds} s.`
                : ""}
            </Alert>
          </div>
        )}
      </Card>

      {answer && !error && (
        <Card className="mt-6">
          <p className="mb-4 text-sm font-medium text-slate-500">{asked}</p>
          <AnswerView answer={answer} />
        </Card>
      )}
    </>
  );
}
