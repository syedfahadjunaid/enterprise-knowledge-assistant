import { useState } from "react";

import { type AnswerState, citedSources } from "../lib/answer";
import { Badge, Spinner } from "./ui";

/**
 * Renders a streamed answer and its sources.
 *
 * Everything from the model and from documents is rendered as plain text (React escapes it).
 * Never use dangerouslySetInnerHTML here: a document could contain HTML or script.
 */
export function AnswerView({ answer }: { answer: AnswerState }) {
  const [highlighted, setHighlighted] = useState<number | null>(null);
  const cited = new Set(citedSources(answer));

  return (
    <div className="space-y-6">
      <div aria-live="polite" className="text-[15px] leading-relaxed whitespace-pre-wrap text-slate-800">
        {answer.segments.map((segment, i) => (
          <span key={i}>
            {segment.text}
            {segment.sources.map((source) => (
              <button
                key={source}
                type="button"
                onClick={() => setHighlighted(source)}
                className="mx-0.5 inline-flex -translate-y-1 items-center rounded bg-indigo-50 px-1 text-[11px] font-semibold text-indigo-700 ring-1 ring-indigo-200 hover:bg-indigo-100"
                aria-label={`Source ${source + 1}`}
              >
                {source + 1}
              </button>
            ))}
          </span>
        ))}
        {answer.status === "streaming" && answer.segments.length === 0 && <Spinner label="Searching your documents" />}
      </div>

      {answer.status === "error" && <p className="text-sm text-red-700">{answer.error}</p>}
      {answer.status === "done" && answer.noLlmCall && (
        <p className="text-xs text-slate-500">No matching passages, so no AI model was called.</p>
      )}

      {answer.sources.length > 0 && (
        <section aria-label="Sources">
          <h2 className="mb-2 text-sm font-semibold text-slate-700">
            Sources <span className="font-normal text-slate-500">(only documents you can access)</span>
          </h2>
          <ol className="space-y-2">
            {answer.sources.map((source) => (
              <li
                key={source.chunk_id}
                className={`rounded-lg p-3 text-sm ring-1 ${
                  highlighted === source.index ? "bg-indigo-50 ring-indigo-300" : "bg-white ring-slate-200"
                }`}
              >
                <div className="mb-1 flex flex-wrap items-center gap-2">
                  <span className="font-semibold text-slate-500">{source.index + 1}.</span>
                  <span className="font-medium">{source.document_title}</span>
                  {source.heading && <span className="text-slate-500">› {source.heading}</span>}
                  {source.page && <Badge>page {source.page}</Badge>}
                  {cited.has(source.index) ? <Badge tone="indigo">cited</Badge> : <Badge>retrieved</Badge>}
                  <span className="ml-auto text-xs text-slate-400">score {source.score.toFixed(2)}</span>
                </div>
                <p className="whitespace-pre-wrap text-slate-600">{source.text}</p>
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}
