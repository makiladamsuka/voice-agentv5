"use client";

import React, { useState, useEffect, useCallback } from "react";
import {
  BookOpen,
  Save,
  RefreshCw,
  CheckCircle2,
  AlertCircle,
  Loader2,
  Info,
  Plus,
  ChevronDown,
  ChevronUp,
} from "lucide-react";

type Status = "idle" | "loading" | "saving" | "success" | "error";

// ── Minimal YAML topic parser for the card editor ──────────────────────────
// We parse only the fields we display. Raw YAML is always sent to the server.

type Topic = {
  topic: string;
  aliases: string[];
  reply: string;
};

function parseTopicsFromYaml(yaml: string): Topic[] {
  const topics: Topic[] = [];
  const blocks = yaml.split(/^- topic:/m).slice(1);
  for (const block of blocks) {
    const topicMatch = block.match(/^\s*(\S+)/);
    const topic = topicMatch ? topicMatch[1].trim() : "";

    const aliasBlock = block.match(/aliases:\s*\n([\s\S]*?)(?=\n\s*\w)/);
    const aliases: string[] = [];
    if (aliasBlock) {
      const lines = aliasBlock[1].split("\n");
      for (const l of lines) {
        const m = l.match(/^\s*-\s*(.+)/);
        if (m) aliases.push(m[1].trim().replace(/^['"]|['"]$/g, ""));
      }
    }

    const replyMatch = block.match(/reply:\s*[>|]?\s*\n([\s\S]*?)(?=\n- topic:|\n$|$)/);
    const reply = replyMatch
      ? replyMatch[1]
          .split("\n")
          .map((l) => l.trim())
          .filter(Boolean)
          .join(" ")
      : "";

    if (topic) topics.push({ topic, aliases, reply });
  }
  return topics;
}

function topicsToYaml(topics: Topic[]): string {
  return topics
    .map((t) => {
      const aliases = t.aliases
        .map((a) => `    - ${a}`)
        .join("\n");
      return `- topic: ${t.topic}\n  aliases:\n${aliases}\n  reply: >\n    ${t.reply}\n`;
    })
    .join("\n");
}

// ── TopicCard component ─────────────────────────────────────────────────────
function TopicCard({
  topic,
  onUpdate,
  onDelete,
}: {
  topic: Topic;
  onUpdate: (updated: Topic) => void;
  onDelete: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [local, setLocal] = useState(topic);

  const update = (patch: Partial<Topic>) => {
    const updated = { ...local, ...patch };
    setLocal(updated);
    onUpdate(updated);
  };

  const setAlias = (i: number, val: string) => {
    const aliases = [...local.aliases];
    aliases[i] = val;
    update({ aliases });
  };

  const addAlias = () => update({ aliases: [...local.aliases, ""] });
  const removeAlias = (i: number) => {
    const aliases = local.aliases.filter((_, idx) => idx !== i);
    update({ aliases });
  };

  return (
    <div className="bg-white/5 border border-white/10 rounded-2xl overflow-hidden transition-all">
      {/* Header row */}
      <button
        onClick={() => setExpanded((p) => !p)}
        className="w-full flex items-center justify-between px-5 py-4 hover:bg-white/5 transition-colors"
      >
        <div className="flex items-center gap-3">
          <span className="text-blue-400 font-mono text-xs bg-blue-500/10 px-2 py-0.5 rounded-full">
            {local.topic}
          </span>
          <span className="text-sm text-white/60 line-clamp-1 max-w-xs text-left">
            {local.reply.slice(0, 80)}…
          </span>
        </div>
        {expanded ? (
          <ChevronUp className="w-4 h-4 text-white/40 shrink-0" />
        ) : (
          <ChevronDown className="w-4 h-4 text-white/40 shrink-0" />
        )}
      </button>

      {/* Body */}
      {expanded && (
        <div className="px-5 pb-5 space-y-4 border-t border-white/10">
          {/* Topic ID */}
          <div className="pt-4">
            <label className="text-xs font-semibold text-white/50 uppercase tracking-wider mb-1 block">
              Topic ID
            </label>
            <input
              value={local.topic}
              onChange={(e) => update({ topic: e.target.value.replace(/\s/g, "_") })}
              className="w-full bg-white/10 rounded-xl px-4 py-2.5 text-sm text-white placeholder:text-white/30 border border-white/10 focus:outline-none focus:border-blue-500/60"
              placeholder="e.g. faculty_of_it"
            />
          </div>

          {/* Aliases */}
          <div>
            <label className="text-xs font-semibold text-white/50 uppercase tracking-wider mb-2 block">
              Trigger Phrases (what visitors might say)
            </label>
            <div className="space-y-2">
              {local.aliases.map((alias, i) => (
                <div key={i} className="flex gap-2">
                  <input
                    value={alias}
                    onChange={(e) => setAlias(i, e.target.value)}
                    className="flex-1 bg-white/10 rounded-xl px-4 py-2 text-sm text-white placeholder:text-white/30 border border-white/10 focus:outline-none focus:border-blue-500/60"
                    placeholder="e.g. tell me about FIT"
                  />
                  <button
                    onClick={() => removeAlias(i)}
                    className="px-3 py-2 text-xs text-red-400 hover:bg-red-500/10 rounded-xl transition-colors"
                  >
                    ✕
                  </button>
                </div>
              ))}
              <button
                onClick={addAlias}
                className="flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 transition-colors"
              >
                <Plus className="w-4 h-4" /> Add phrase
              </button>
            </div>
          </div>

          {/* Reply */}
          <div>
            <label className="text-xs font-semibold text-white/50 uppercase tracking-wider mb-1 block">
              Robot Reply (1–2 sentences)
            </label>
            <textarea
              value={local.reply}
              onChange={(e) => update({ reply: e.target.value })}
              rows={3}
              className="w-full bg-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder:text-white/30 border border-white/10 focus:outline-none focus:border-blue-500/60 resize-none"
              placeholder="What the robot says when asked this question…"
            />
          </div>

          {/* Delete */}
          <div className="flex justify-end">
            <button
              onClick={onDelete}
              className="text-xs text-red-400 hover:bg-red-500/10 px-4 py-2 rounded-xl transition-colors"
            >
              Delete topic
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Main page ──────────────────────────────────────────────────────────────

export default function KnowledgeBasePage() {
  const [status, setStatus] = useState<Status>("loading");
  const [error, setError] = useState("");
  const [topics, setTopics] = useState<Topic[]>([]);
  const [view, setView] = useState<"cards" | "yaml">("cards");
  const [rawYaml, setRawYaml] = useState("");
  const [saveMsg, setSaveMsg] = useState("");

  // Fetch current knowledge base
  const load = useCallback(async () => {
    setStatus("loading");
    setError("");
    try {
      const res = await fetch("/api/knowledge");
      if (!res.ok) throw new Error("Failed to load knowledge base");
      const data = await res.json();
      setRawYaml(data.yaml || "");
      setTopics(parseTopicsFromYaml(data.yaml || ""));
      setStatus("idle");
    } catch (err: any) {
      setError(err.message);
      setStatus("error");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleSave = async () => {
    setStatus("saving");
    setSaveMsg("");
    setError("");
    const yaml = view === "cards" ? topicsToYaml(topics) : rawYaml;
    try {
      const res = await fetch("/api/knowledge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ yaml }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Save failed");
      setStatus("success");
      setSaveMsg(data.message || "Saved!");
      // Re-parse to sync card state
      setTopics(parseTopicsFromYaml(yaml));
      setRawYaml(yaml);
      setTimeout(() => setStatus("idle"), 3000);
    } catch (err: any) {
      setError(err.message);
      setStatus("error");
    }
  };

  const addTopic = () => {
    setTopics((prev) => [
      ...prev,
      { topic: `new_topic_${Date.now()}`, aliases: [""], reply: "" },
    ]);
  };

  const updateTopic = (i: number, updated: Topic) => {
    setTopics((prev) => prev.map((t, idx) => (idx === i ? updated : t)));
  };

  const deleteTopic = (i: number) => {
    setTopics((prev) => prev.filter((_, idx) => idx !== i));
  };

  const isBusy = status === "loading" || status === "saving";

  return (
    <div className="min-h-screen bg-[#0a0a1a] text-white font-sans">
      {/* Header */}
      <div className="border-b border-white/10 bg-white/5 backdrop-blur px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-blue-500/20 flex items-center justify-center">
            <BookOpen className="w-5 h-5 text-blue-400" />
          </div>
          <div>
            <h1 className="text-lg font-bold leading-none">Knowledge Base</h1>
            <p className="text-xs text-white/40 mt-0.5">
              What the robot knows about UOM &amp; FIT
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button
            onClick={load}
            disabled={isBusy}
            className="p-2 rounded-xl hover:bg-white/10 transition-colors"
            title="Refresh"
          >
            <RefreshCw className={`w-4 h-4 text-white/60 ${status === "loading" ? "animate-spin" : ""}`} />
          </button>

          {/* View toggle */}
          <div className="flex bg-white/5 rounded-xl p-1 gap-1 border border-white/10">
            {(["cards", "yaml"] as const).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                  view === v
                    ? "bg-blue-500 text-white"
                    : "text-white/50 hover:text-white"
                }`}
              >
                {v === "cards" ? "Visual" : "YAML"}
              </button>
            ))}
          </div>

          <button
            onClick={handleSave}
            disabled={isBusy}
            className={`flex items-center gap-2 px-5 py-2.5 rounded-xl font-semibold text-sm transition-all ${
              status === "success"
                ? "bg-green-500 text-white"
                : isBusy
                ? "bg-blue-500/50 text-white/50 cursor-not-allowed"
                : "bg-blue-500 hover:bg-blue-400 text-white shadow-lg shadow-blue-500/20"
            }`}
          >
            {status === "saving" ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : status === "success" ? (
              <CheckCircle2 className="w-4 h-4" />
            ) : (
              <Save className="w-4 h-4" />
            )}
            {status === "saving"
              ? "Saving…"
              : status === "success"
              ? "Saved!"
              : "Save & Apply"}
          </button>
        </div>
      </div>

      <div className="max-w-4xl mx-auto px-6 py-8 space-y-6">
        {/* Info banner */}
        <div className="flex gap-3 bg-blue-500/10 border border-blue-500/20 rounded-2xl p-4">
          <Info className="w-5 h-5 text-blue-400 shrink-0 mt-0.5" />
          <div className="text-sm text-white/70 space-y-1">
            <p>
              <span className="font-semibold text-white">How this works:</span>{" "}
              Add topics the robot should know about. Each topic has trigger
              phrases and a reply. Clicking{" "}
              <span className="font-semibold text-white">"Save &amp; Apply"</span>{" "}
              compiles the changes and reloads the robot — no restart needed.
            </p>
          </div>
        </div>

        {/* Error banner */}
        {status === "error" && error && (
          <div className="flex gap-3 bg-red-500/10 border border-red-500/20 rounded-2xl p-4">
            <AlertCircle className="w-5 h-5 text-red-400 shrink-0 mt-0.5" />
            <p className="text-sm text-red-300">{error}</p>
          </div>
        )}

        {/* Success banner */}
        {status === "success" && saveMsg && (
          <div className="flex gap-3 bg-green-500/10 border border-green-500/20 rounded-2xl p-4">
            <CheckCircle2 className="w-5 h-5 text-green-400 shrink-0 mt-0.5" />
            <p className="text-sm text-green-300">{saveMsg}</p>
          </div>
        )}

        {status === "loading" ? (
          <div className="flex items-center justify-center py-20 gap-3 text-white/40">
            <Loader2 className="w-6 h-6 animate-spin" />
            <span>Loading knowledge base…</span>
          </div>
        ) : view === "cards" ? (
          <>
            {/* Topic cards */}
            <div className="space-y-3">
              {topics.map((t, i) => (
                <TopicCard
                  key={t.topic + i}
                  topic={t}
                  onUpdate={(updated) => updateTopic(i, updated)}
                  onDelete={() => deleteTopic(i)}
                />
              ))}
            </div>

            {/* Add new topic */}
            <button
              onClick={addTopic}
              className="w-full flex items-center justify-center gap-2 py-4 border-2 border-dashed border-white/20 rounded-2xl text-white/50 hover:text-white hover:border-blue-500/40 hover:bg-blue-500/5 transition-all text-sm font-medium"
            >
              <Plus className="w-4 h-4" />
              Add new topic
            </button>
          </>
        ) : (
          /* Raw YAML editor */
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <p className="text-xs text-white/40 font-mono">
                voice/knowledge_base.yaml
              </p>
              <p className="text-xs text-white/30">
                {rawYaml.split("\n").length} lines
              </p>
            </div>
            <textarea
              value={rawYaml}
              onChange={(e) => setRawYaml(e.target.value)}
              className="w-full h-[60vh] bg-black/40 rounded-2xl p-5 font-mono text-sm text-green-300 border border-white/10 focus:outline-none focus:border-blue-500/40 resize-none leading-relaxed"
              spellCheck={false}
            />
          </div>
        )}
      </div>
    </div>
  );
}
