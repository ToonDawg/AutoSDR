import { Bot, Clock3, User as UserIcon, UserCog } from "lucide-react";
import { MessageRole, type Message } from "@/lib/types";
import { absTime, INTENT_LABEL } from "@/lib/format";
import { cn } from "@/lib/utils";

export function MessageBubble({
  message,
  leadName,
}: {
  message: Message;
  leadName: string;
}) {
  const isAI = message.role === MessageRole.AI;
  const isLead = message.role === MessageRole.LEAD;
  const isHuman = message.role === MessageRole.HUMAN;
  const isFollowup = message.metadata.source === "followup";

  return (
    <div
      className={cn(
        "group flex flex-col gap-1.5 max-w-full",
        isLead ? "items-end" : "items-start",
      )}
    >
      <div className="flex items-center gap-2 text-[11px] font-mono uppercase tracking-[0.14em] text-ink-muted">
        {isAI && (
          <>
            {isFollowup ? (
              <Clock3 className="h-3 w-3" strokeWidth={1.5} />
            ) : (
              <Bot className="h-3 w-3" strokeWidth={1.5} />
            )}
            <span>{isFollowup ? "Follow-up" : "AutoSDR"}</span>
          </>
        )}
        {isHuman && (
          <>
            <UserCog className="h-3 w-3" strokeWidth={1.5} />
            <span>You</span>
          </>
        )}
        {isLead && (
          <>
            <UserIcon className="h-3 w-3" strokeWidth={1.5} />
            <span>{leadName}</span>
          </>
        )}
        <span className="text-ink-faint">·</span>
        <span className="text-ink-faint normal-case tracking-normal">
          {absTime(message.created_at)}
        </span>
      </div>
      <div
        className={cn(
          "bubble",
          isAI && "bubble-ai",
          isLead && "bubble-lead",
          isHuman && "bubble-human",
        )}
      >
        {message.content}
      </div>

      {/* Metadata ribbon */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] font-mono uppercase tracking-[0.14em] text-ink-faint">
        {isFollowup &&
          typeof message.metadata.scheduled_delay_s === "number" && (
            <span>
              beat{" "}
              <span className="text-ink-muted">
                +{Math.round(message.metadata.scheduled_delay_s)}s
              </span>
            </span>
          )}
        {typeof message.metadata.eval_score === "number" && (
          <span>
            eval{" "}
            <span className="text-ink-muted">
              {Math.round(message.metadata.eval_score * 100)}
            </span>
          </span>
        )}
        {message.metadata.attempt_count != null &&
          message.metadata.attempt_count > 1 && (
            <span>
              attempts{" "}
              <span className="text-ink-muted">
                {message.metadata.attempt_count}
              </span>
            </span>
          )}
        {message.metadata.prompt_version && (
          <span>
            prompt{" "}
            <span className="text-ink-muted">
              {message.metadata.prompt_version}
            </span>
          </span>
        )}
        {message.metadata.model && (
          <span>
            model{" "}
            <span className="text-ink-muted normal-case">
              {message.metadata.model}
            </span>
          </span>
        )}
        {message.metadata.intent && (
          <span>
            intent{" "}
            <span className="text-rust">
              {INTENT_LABEL[message.metadata.intent]}
            </span>
            {message.metadata.confidence != null && (
              <span className="text-ink-muted">
                {" "}
                · {Math.round(message.metadata.confidence * 100)}%
              </span>
            )}
          </span>
        )}
      </div>
    </div>
  );
}
