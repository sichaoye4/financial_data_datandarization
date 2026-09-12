import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "../styles.css";

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || "The request could not be completed.");
  return body;
}

async function encodeFile(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return { filename: file.name, content_base64: btoa(binary) };
}

function titleCase(value) {
  return String(value ?? "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatValue(value) {
  if (typeof value === "number") return value.toLocaleString("en-SG", { maximumFractionDigits: 2 });
  return value ?? "—";
}

function statusClass(status) {
  return { accepted: "good", published: "good", changed: "changed", proposed: "pending" }[status] || "pending";
}

function DataTable({ rows = [], columns = [], target = false, changedField = "" }) {
  const visibleColumns = rows.length ? Object.keys(rows[0]) : columns;
  if (!visibleColumns.length) return <p className="empty">No columns available.</p>;
  return (
    <div className="table-scroll">
      <table className={target ? "target-table" : ""}>
        <thead><tr>{visibleColumns.map((column) => <th key={column}>{target ? titleCase(column) : column}</th>)}</tr></thead>
        <tbody>
          {rows.length ? rows.map((row, rowIndex) => (
            <tr key={rowIndex}>{visibleColumns.map((column) => (
              <td className={changedField === column ? "cell-changed" : ""} key={column}>{formatValue(row[column])}</td>
            ))}</tr>
          )) : <tr><td className="empty-cell" colSpan={visibleColumns.length}>Header-only target example</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

function ProviderStatus({ provider }) {
  return (
    <div className={`provider-status-card ${provider?.configured ? "ready" : "missing"}`}>
      <span className="provider-dot" />
      <div>
        <strong>{provider?.configured ? `DeepSeek ready · ${provider.model}` : "DeepSeek credential not configured"}</strong>
        <small>Thinking {provider?.thinking || "enabled"} · credentials stay in the Python backend</small>
      </div>
    </div>
  );
}

function FileSection({ role, file, inputRef, onFile }) {
  const isSource = role === "source";
  return (
    <section className={`file-section ${role}`}>
      <div className="file-section-number">{isSource ? "1" : "2"}</div>
      <div>
        <p className="eyebrow">{isSource ? "Source data" : "Target example"}</p>
        <h2>{isSource ? "Upload the data to standardize" : "Upload the expected standardized format"}</h2>
        <p>{isSource
          ? "CSV or Excel with arbitrary headers and layouts. Every Excel sheet becomes a source table."
          : "CSV or Excel showing the required tables, headers and value types. Every Excel sheet becomes a target table."}</p>
        <button className="upload-zone compact" onClick={() => inputRef.current?.click()}>
          <span className="upload-icon">↥</span>
          <strong>{file ? file.name : `Choose ${isSource ? "source" : "target example"} file`}</strong>
          <small>CSV, XLSX, XLSM or XLS · maximum 8 MB</small>
        </button>
        <input
          ref={inputRef}
          type="file"
          accept=".csv,.xlsx,.xlsm,.xls,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel"
          hidden
          onChange={(event) => onFile(event.target.files?.[0] || null)}
        />
      </div>
    </section>
  );
}

function UploadStage({ provider, busy, onCreate }) {
  const [sourceFile, setSourceFile] = useState(null);
  const [targetFile, setTargetFile] = useState(null);
  const sourceRef = useRef(null);
  const targetRef = useRef(null);
  return (
    <main className="upload-shell paired-upload-shell">
      <section className="upload-card paired-upload-card">
        <div className="upload-brand"><span className="brand-mark">F</span><span>Financial Data Standardization Workbench</span></div>
        <p className="eyebrow">Paired document intake</p>
        <h1>Upload the source and target example together.</h1>
        <p className="upload-copy">The source contains the data you have. The target example defines the standardized tables and columns you expect.</p>
        <div className="file-pair-grid">
          <FileSection role="source" file={sourceFile} inputRef={sourceRef} onFile={setSourceFile} />
          <FileSection role="target" file={targetFile} inputRef={targetRef} onFile={setTargetFile} />
        </div>
        <div className="pair-submit">
          <span>{sourceFile && targetFile ? "Both sides selected" : "Select one source and one target example"}</span>
          <button className="primary" disabled={!sourceFile || !targetFile || busy === "upload"} onClick={() => onCreate(sourceFile, targetFile)}>
            {busy === "upload" ? "Reading tables…" : "Continue to table matching"}
          </button>
        </div>
        <ProviderStatus provider={provider} />
      </section>
    </main>
  );
}

function WorkflowProgress({ active }) {
  const labels = ["Upload pair", "Match tables", "Map fields", "Implementation"];
  return (
    <section className="progress" aria-label="Mapping workflow">
      {labels.map((label, index) => <React.Fragment key={label}>
        <span className={index + 1 < active ? "complete" : index + 1 === active ? "active" : ""}>{index + 1} <b>{label}</b></span>
        {index < labels.length - 1 && <i />}
      </React.Fragment>)}
    </section>
  );
}

function AiProgress({ title, detail }) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setElapsed((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return (
    <div className="ai-progress" role="status">
      <span className="spinner" />
      <div><strong>{title}</strong><small>{detail} · {elapsed}s elapsed</small></div>
    </div>
  );
}

function AiError({ message, onRetry }) {
  return (
    <div className="ai-error" role="alert">
      <div><strong>DeepSeek did not return a usable suggestion</strong><small>{message}</small></div>
      <button className="subtle" onClick={onRetry}>Retry initial suggestion</button>
    </div>
  );
}

function TableCard({ table, tone, draggable = false, onDragStart, onPointerDown, assignedCount = 0 }) {
  return (
    <article
      className={`table-card ${tone}${draggable ? " draggable" : ""}`}
      draggable={draggable}
      onDragStart={onDragStart}
      onPointerDown={onPointerDown}
    >
      <div className="table-card-heading">
        <div><strong>{table.name}</strong><small>{table.filename}</small></div>
        <span>{table.row_count} rows · {table.column_count} columns</span>
      </div>
      <div className="detected-range">Detected {table.detected_region.range} · header row {table.detected_region.header_row}</div>
      <div className="column-chips">{table.columns.map((column) => <span key={column}>{column}</span>)}</div>
      {draggable && <div className="drag-hint"><span>⋮⋮</span> Drag onto a target table{assignedCount ? ` · used ${assignedCount}×` : ""}</div>}
    </article>
  );
}

function TableMappingStage({ session, busy, aiError, onPropose, onConfirm, onReset }) {
  const [selections, setSelections] = useState({});
  const [comment, setComment] = useState("");
  const [dragOverTarget, setDragOverTarget] = useState("");
  const pointerDragSource = useRef("");
  useEffect(() => {
    setSelections(Object.fromEntries(session.table_mapping_proposal.map((item) => [item.target_table_id, item.source_table_id])));
  }, [session.table_mapping_proposal]);
  useEffect(() => {
    const clearPointerDrag = () => {
      pointerDragSource.current = "";
      setDragOverTarget("");
    };
    window.addEventListener("pointerup", clearPointerDrag);
    window.addEventListener("pointercancel", clearPointerDrag);
    return () => {
      window.removeEventListener("pointerup", clearPointerDrag);
      window.removeEventListener("pointercancel", clearPointerDrag);
    };
  }, []);

  const sourceById = Object.fromEntries(session.source_tables.map((item) => [item.id, item]));
  const proposalByTarget = Object.fromEntries(session.table_mapping_proposal.map((item) => [item.target_table_id, item]));
  const assignmentCounts = Object.values(selections).reduce((counts, sourceId) => ({ ...counts, [sourceId]: (counts[sourceId] || 0) + 1 }), {});
  const allTargetsAssigned = session.target_tables.every((target) => Boolean(selections[target.id]));
  const assign = (targetId, sourceId) => setSelections((current) => ({ ...current, [targetId]: sourceId }));
  const remove = (targetId) => setSelections((current) => {
    const next = { ...current };
    delete next[targetId];
    return next;
  });
  const dropSource = (event, targetId) => {
    event.preventDefault();
    const sourceId = event.dataTransfer.getData("text/plain");
    if (sourceById[sourceId]) assign(targetId, sourceId);
    setDragOverTarget("");
  };

  return (
    <main className="stage-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">F</span><div><strong>Financial Data Standardization Workbench</strong><small>Paired documents · table-first review</small></div></div>
        <div className="topbar-actions"><span className="provider-pill live"><i />DeepSeek · thinking enabled</span><button className="subtle" onClick={onReset}>New upload pair</button></div>
      </header>
      <WorkflowProgress active={2} />
      <section className="table-stage-heading">
        <div><p className="eyebrow">Step 2 · Table matching</p><h1>Drag source tables onto their target output tables.</h1><p>DeepSeek proposes the first set automatically. You stay in control of every pair.</p></div>
        <span className={`auto-ai-badge ${busy === "table-ai" ? "working" : ""}`}><span>{busy === "table-ai" ? "↻" : "✦"}</span>{busy === "table-ai" ? "Building initial matches" : "Initial AI matching is automatic"}</span>
      </section>
      <section className="table-pair-layout">
        <aside className="table-inventory source-palette">
          <div className="inventory-title"><p className="eyebrow">Draggable source tables</p><h2>{session.source_document}</h2><span>{session.source_tables.length} table{session.source_tables.length === 1 ? "" : "s"}</span></div>
          <p className="drag-guidance">Drag any source table to a target card. A source can feed more than one target.</p>
          <div className="source-table-list">{session.source_tables.map((table) => (
            <TableCard
              table={table}
              tone="source"
              key={table.id}
              draggable
              assignedCount={assignmentCounts[table.id] || 0}
              onDragStart={(event) => {
                event.dataTransfer.effectAllowed = "copy";
                event.dataTransfer.setData("text/plain", table.id);
              }}
              onPointerDown={(event) => {
                if (event.button === 0) pointerDragSource.current = table.id;
              }}
            />
          ))}</div>
        </aside>
        <section className="pairing-workspace">
          <div className="pairing-heading">
            <div><p className="eyebrow">Target pairing board</p><h2>{session.target_document}</h2></div>
            <span>{Object.keys(selections).length}/{session.target_tables.length} paired</span>
          </div>
          {busy === "table-ai" && !session.table_mapping_proposal.length && <AiProgress title="DeepSeek is comparing all tables" detail="Names, headers, types and representative values are being reviewed" />}
          {aiError?.scope === "table" && !session.table_mapping_proposal.length && <AiError message={aiError.message} onRetry={() => onPropose("")} />}
          <div className="pairing-grid">{session.target_tables.map((target) => {
            const suggestion = proposalByTarget[target.id];
            const source = sourceById[selections[target.id]];
            const changedFromAi = Boolean(suggestion && source && suggestion.source_table_id !== source.id);
            return (
              <article
                className={`pair-drop-card${source ? " paired" : " unpaired"}${dragOverTarget === target.id ? " drag-over" : ""}`}
                key={target.id}
                onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; setDragOverTarget(target.id); }}
                onDragLeave={() => setDragOverTarget("")}
                onDrop={(event) => dropSource(event, target.id)}
                onPointerEnter={(event) => {
                  if (event.buttons === 1 && pointerDragSource.current) setDragOverTarget(target.id);
                }}
                onPointerUp={() => {
                  const sourceId = pointerDragSource.current;
                  if (sourceById[sourceId]) assign(target.id, sourceId);
                }}
              >
                <div className="target-card-heading"><div><p className="eyebrow">Target table</p><h3>{target.name}</h3></div><span>{target.row_count} rows · {target.column_count} columns</span></div>
                <div className="detected-range target-range">Detected {target.detected_region.range} · header row {target.detected_region.header_row}</div>
                <div className="column-chips target-chips">{target.columns.map((column) => <span key={column}>{column}</span>)}</div>
                <div className="pair-direction"><span>Source input</span><i>→</i><span>Standardized target</span></div>
                {source ? (
                  <div className="assigned-source">
                    <div><strong>{source.name}</strong><small>{source.column_count} columns · {source.row_count} rows</small></div>
                    <button className="remove-pair" onClick={() => remove(target.id)} aria-label={`Remove source assignment for ${target.name}`}>Remove</button>
                  </div>
                ) : (
                  <div className="empty-drop"><strong>Drop a source table here</strong><small>or choose one below</small></div>
                )}
                <label className="source-select" htmlFor={`source-${target.id}`}><span>Source table</span>
                  <select id={`source-${target.id}`} value={source?.id || ""} onChange={(event) => event.target.value ? assign(target.id, event.target.value) : remove(target.id)}>
                    <option value="">Not paired</option>
                    {session.source_tables.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
                  </select>
                </label>
                {suggestion && <div className={`ai-pair-note ${changedFromAi ? "changed" : ""}`}><strong>{changedFromAi ? "Changed by you" : `${Math.round(suggestion.confidence * 100)}% AI confidence`}</strong><p>{suggestion.rationale}</p></div>}
              </article>
            );
          })}</div>
          {session.table_mapping_proposal.length > 0 && (
            <section className="table-ai-revision">
              <div><p className="eyebrow">Want a different AI proposal?</p><h3>Tell DeepSeek what to change, then regenerate.</h3></div>
              <textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Example: Map the Account Master target to the source tab containing account codes." />
              <button className="subtle" onClick={() => onPropose(comment)} disabled={!comment.trim() || busy === "table-ai"}>{busy === "table-ai" ? "Regenerating…" : "Regenerate from comment"}</button>
            </section>
          )}
          {aiError?.scope === "table" && session.table_mapping_proposal.length > 0 && <AiError message={aiError.message} onRetry={() => onPropose(comment)} />}
          <div className="confirm-pairs-bar"><span>{allTargetsAssigned ? "Every target has a source table." : `${session.target_tables.length - Object.keys(selections).length} target table(s) still need a source.`}</span><button className="primary" disabled={!session.table_mapping_proposal.length || !allTargetsAssigned || Boolean(busy)} onClick={() => onConfirm(selections)}>{busy === "table-confirm" ? "Confirming…" : "Confirm pairs and review fields"}</button></div>
        </section>
      </section>
    </main>
  );
}

function MappingCard({ step, active, checked, onSelect, onToggle }) {
  const review = step.review || {};
  const operation = step.function ? `${step.op} · ${step.function}` : step.op;
  const inputs = step.inputs?.map((input) => input.column).join(" + ") || "not mapped";
  const selectable = step.op !== "unmapped" && !review.locked;
  return (
    <article className={`mapping-card${active ? " selected" : ""}${checked ? " bulk-selected" : ""}`}>
      <input
        className="mapping-checkbox"
        type="checkbox"
        aria-label={`Select ${titleCase(step.target)}`}
        checked={checked}
        disabled={!selectable}
        onChange={() => onToggle(step.target)}
      />
      <button className="mapping-card-body" onClick={() => onSelect(step.target)} aria-pressed={active}>
        <span className="mapping-card-top"><span className="field-name">{titleCase(step.target)}</span><span className={`status ${statusClass(review.status)}`}>{review.status || "empty"}</span></span>
        <span className="mapping-line">{inputs} <span>→</span> {operation}</span>
        <span className="mapping-meta"><span>Confidence {Math.round((review.confidence || 0) * 100)}%</span><span>{review.locked ? "Locked" : "Review needed"}</span></span>
      </button>
    </article>
  );
}

function Issue({ issue }) {
  return <li className={`issue ${issue.severity}`}><span className="issue-icon">{issue.severity === "error" ? "!" : "△"}</span><span><strong>{titleCase(issue.field)}</strong><br />{issue.message}</span></li>;
}

function ProposalModal({ proposal, beforeRows, field, onAccept, onDismiss, busy }) {
  return (
    <div className="modal-backdrop" role="presentation"><section className="modal" role="dialog" aria-modal="true">
      <div className="modal-heading"><div><p className="eyebrow">Proposed revision</p><h2>{titleCase(field)} mapping revised</h2></div><button className="icon-button" onClick={onDismiss}>×</button></div>
      <p>{proposal.explanation}</p>
      <div className="diff-grid"><div><p className="eyebrow">Before</p><strong>{formatValue(beforeRows[0]?.[field])}</strong><small>Current validated rule</small></div><div className="arrow">→</div><div className="after"><p className="eyebrow">After</p><strong>{formatValue(proposal.proposed.preview.rows[0]?.[field])}</strong><small>Proposed DeepSeek rule</small></div></div>
      <pre>{JSON.stringify(proposal.patch, null, 2)}</pre>
      <div className="modal-actions"><button className="subtle" onClick={onDismiss}>Reject</button><button className="primary" onClick={onAccept} disabled={busy}>{busy ? "Accepting…" : "Accept and lock rule"}</button></div>
    </section></div>
  );
}

function ImplementationStage({ implementation, onReset }) {
  const [activePairId, setActivePairId] = useState(implementation.pairs[0]?.mapping_id || "");
  const activePair = implementation.pairs.find((pair) => pair.mapping_id === activePairId) || implementation.pairs[0];
  return (
    <main className="stage-shell implementation-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">F</span><div><strong>Financial Data Standardization Workbench</strong><small>Persistent mapping implementation</small></div></div>
        <div className="topbar-actions"><span className="status-pill ready">Saved in database</span><button className="subtle" onClick={onReset}>New upload pair</button></div>
      </header>
      <WorkflowProgress active={4} />
      <section className="implementation-heading">
        <div><p className="eyebrow">Implementation preview</p><h1>Saved mapping and deterministic Pandas code</h1><p>{implementation.source_document} → {implementation.target_document}</p></div>
        <div className="saved-meta"><strong>{implementation.pairs.length} table pair{implementation.pairs.length === 1 ? "" : "s"}</strong><small>Saved {new Date(implementation.saved_at).toLocaleString()}</small></div>
      </section>
      <section className="implementation-layout">
        <aside className="implementation-pairs">
          <p className="eyebrow">Saved table pairs</p>
          {implementation.pairs.map((pair) => <button className={pair.mapping_id === activePair?.mapping_id ? "active" : ""} onClick={() => setActivePairId(pair.mapping_id)} key={pair.mapping_id}><strong>{pair.source_table.name} → {pair.target_table.name}</strong><small>{pair.definition.steps.length} fields · {pair.validation.status}</small></button>)}
        </aside>
        {activePair && <section className="implementation-detail">
          <div className="implementation-summary"><div><p className="eyebrow">Table mapping</p><h2>{activePair.source_table.name} → {activePair.target_table.name}</h2></div><span className="status-pill ready">Validated</span></div>
          <div className="implementation-regions"><span>Source {activePair.source_table.detected_region.range}</span><span>Target {activePair.target_table.detected_region.range}</span><span>{activePair.preview.metrics.target_rows} preview rows</span></div>
          <div className="implementation-code-grid">
            <section><div className="code-heading"><p className="eyebrow">Authoritative mapping</p><strong>JSON definition</strong></div><pre>{JSON.stringify(activePair.definition, null, 2)}</pre></section>
            <section><div className="code-heading"><p className="eyebrow">Generated implementation</p><strong>Pandas / Python</strong></div><pre><code>{activePair.generated_pandas}</code></pre></section>
          </div>
        </section>}
      </section>
    </main>
  );
}

function FieldMappingStage({ session, busy, aiError, proposal, selectedField, bulkSelectedFields, setBulkSelectedFields, comment, setComment, setSelectedField, actions }) {
  const selected = session.definition.steps.find((step) => step.target === selectedField) || session.definition.steps[0];
  const validation = session.validation;
  const canSave = session.overall_validation.status === "ready";
  const editable = selected?.op !== "unmapped" && !selected?.review?.locked;
  const acceptable = editable && ["proposed", "changed"].includes(selected?.review?.status);
  const eligibleFields = session.definition.steps.filter((step) => step.op !== "unmapped" && !step.review?.locked).map((step) => step.target);
  const selectedEligible = eligibleFields.filter((field) => bulkSelectedFields.includes(field));
  const toggleBulk = (field) => setBulkSelectedFields((current) => current.includes(field) ? current.filter((item) => item !== field) : [...current, field]);
  return (
    <main className="app-shell" aria-live="polite">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">F</span><div><strong>Financial Data Standardization Workbench</strong><small>Confirmed tables · deterministic field mapping</small></div></div>
        <div className="topbar-actions"><span className="provider-pill live"><i />DeepSeek · {session.provider.model}</span><span className={`status-pill ${canSave ? "ready" : "blocked"}`}>{canSave ? "Ready to save" : `${session.overall_validation.blockers} total blockers`}</span><button className="primary" onClick={actions.saveImplementation} disabled={!canSave || busy === "save"}>{busy === "save" ? "Saving…" : "Save & review implementation"}</button></div>
      </header>
      <WorkflowProgress active={3} />
      <section className="pair-switcher">
        <div><p className="eyebrow">Confirmed table pairs</p><strong>Select a pair to review its field mappings</strong></div>
        <div>{session.table_pairs.map((pair) => <button className={pair.id === session.active_table_mapping_id ? "active" : ""} onClick={() => actions.activate(pair.id)} disabled={Boolean(busy)} key={pair.id}><span>{pair.source_table_name} → {pair.target_table_name}</span><small>{pair.blockers ? `${pair.blockers} blockers` : "Ready"}</small></button>)}</div>
        <button className="subtle" onClick={actions.reset} disabled={busy === "field-ai"}>New upload pair</button>
      </section>
      <section className="workspace">
        <aside className="left-pane">
          <div className="pane-heading"><div><p className="eyebrow">Confirmed source table</p><h1>{session.active_source_table.name}</h1></div></div>
          <div className="data-profile"><span><b>{session.profile.row_count}</b> source rows</span><span><b>{session.profile.columns.length}</b> columns</span><span><b>{session.profile.probable_grain.join(" + ") || "Not inferred"}</b> probable grain</span></div>
          <DataTable rows={session.source_rows} columns={session.active_source_table.columns} />
          <div className="column-profile"><p className="eyebrow">Column profile</p>{session.profile.columns.map((column) => <div className="profile-row" key={column.name}><span>{column.name}</span><span>{column.inferred_type}</span><span>{Math.round(column.null_ratio * 100)}% null</span></div>)}</div>
        </aside>
        <section className="mapping-pane">
          <div className="pane-heading"><div><p className="eyebrow">Field mapping review</p><h2>{session.definition.steps.length} target fields</h2></div><span className={`auto-ai-badge compact ${busy === "field-ai" ? "working" : ""}`}><span>{busy === "field-ai" ? "↻" : "✦"}</span>Automatic first proposal</span></div>
          {busy === "field-ai" ? <AiProgress title="DeepSeek is suggesting field mappings" detail="The page will update as soon as the validated definition is ready" /> : aiError?.scope === "field" && aiError.mappingId === session.active_table_mapping_id ? <AiError message={aiError.message} onRetry={actions.retryFields} /> : <p className="assistant-note"><span>✦</span> {session.mode}. Add a comment below only when you want DeepSeek to revise a field.</p>}
          <div className="bulk-toolbar">
            <span>{selectedEligible.length} selected · {eligibleFields.length} awaiting review</span>
            <div><button className="subtle" onClick={() => setBulkSelectedFields(eligibleFields)} disabled={!eligibleFields.length}>Select all</button><button className="subtle" onClick={() => setBulkSelectedFields([])} disabled={!bulkSelectedFields.length}>Clear all</button><button className="subtle bulk-accept" onClick={() => actions.acceptBulk(selectedEligible, false)} disabled={!selectedEligible.length || busy === "accept-bulk"}>{busy === "accept-bulk" ? "Accepting…" : `Accept selected (${selectedEligible.length})`}</button><button className="primary small" onClick={() => actions.acceptBulk([], true)} disabled={!eligibleFields.length || busy === "accept-bulk"}>Accept all ({eligibleFields.length})</button></div>
          </div>
          <div className="mapping-list">{session.definition.steps.map((step) => <MappingCard key={step.id} step={step} active={step.target === selected?.target} checked={bulkSelectedFields.includes(step.target)} onToggle={toggleBulk} onSelect={(field) => { setSelectedField(field); setComment(""); }} />)}</div>
          <section className="comment-box">
            <div className="comment-heading"><div><p className="eyebrow">Field-scoped correction</p><h3>{titleCase(selected?.target)}</h3></div><span className={`status ${statusClass(selected?.review?.status)}`}>{selected?.review?.status}</span></div>
            <p>{selected?.review?.rationale}</p>
            <textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Describe the field mapping change…" disabled={!editable} />
            <div className="comment-actions"><span>Pair revision v{session.revision}</span><div className="field-actions">{acceptable && <button className="subtle" onClick={() => actions.acceptCurrent(selected.target)} disabled={busy === "accept-current"}>{busy === "accept-current" ? "Accepting…" : "Accept current"}</button>}<button className="primary small" onClick={() => actions.proposeChange(selected.target)} disabled={!editable || busy === "revision"}>{busy === "revision" ? "DeepSeek is thinking…" : "Propose change"}</button></div></div>
          </section>
        </section>
        <section className="right-pane">
          <div className="pane-heading"><div><p className="eyebrow">Target example table</p><h2>{session.active_target_table.name}</h2></div><span className="target-grain">Grain: {session.target_schema.grain.join(" + ")}</span></div>
          <DataTable rows={session.active_target_table.rows} columns={session.active_target_table.columns} target />
          <div className="preview-heading"><p className="eyebrow">Generated standardized preview</p><span>{session.preview.metrics.target_rows} rows</span></div>
          <DataTable rows={session.preview.rows} columns={session.active_target_table.columns} target changedField={proposal ? selected?.target : ""} />
          <section className="lineage-card"><p className="eyebrow">Selected lineage</p><h3>{titleCase(selected?.target)}</h3><p>{selected?.inputs?.map((item) => item.column).join(", ") || "not mapped"}<span>→</span>{selected?.op}{selected?.function ? ` (${selected.function})` : ""}</p><small>{selected?.group_by?.length ? `Grouped by ${selected.group_by.join(" + ")}` : "No group rule declared"}</small></section>
        </section>
      </section>
      <section className="bottom-tray">
        <div className="validation-heading"><div><p className="eyebrow">Validation for active table pair</p><h2>{validation.status === "ready" ? "This table pair is ready" : "Attention required"}</h2></div><button className="subtle" onClick={actions.validate} disabled={busy === "validate"}>{busy === "validate" ? "Validating…" : "Run validation"}</button></div>
        <div className="validation-summary"><span className="issue-count blocker"><b>{validation.blockers}</b> blocking</span><span className="issue-count warning"><b>{validation.warnings}</b> warnings</span><span className="audit-note">Saving waits for every confirmed table pair.</span></div>
        <ul className="issue-list">{validation.issues.length ? validation.issues.map((issue, index) => <Issue issue={issue} key={`${issue.code}-${index}`} />) : <li className="issue success"><span className="issue-icon">✓</span><span><strong>Validated</strong><br />Fields, target grain and review checks passed.</span></li>}</ul>
      </section>
      {proposal && <ProposalModal proposal={proposal} beforeRows={session.preview.rows} field={selected?.target} onAccept={actions.acceptRevision} onDismiss={actions.dismissProposal} busy={busy === "accept"} />}
    </main>
  );
}

function App() {
  const [session, setSession] = useState(null);
  const [provider, setProvider] = useState(null);
  const [selectedField, setSelectedField] = useState("");
  const [bulkSelectedFields, setBulkSelectedFields] = useState([]);
  const [comment, setComment] = useState("");
  const [proposal, setProposal] = useState(null);
  const [implementation, setImplementation] = useState(null);
  const [toast, setToast] = useState("");
  const [busy, setBusy] = useState("");
  const [fatal, setFatal] = useState("");
  const [aiError, setAiError] = useState(null);
  const autoRequests = useRef(new Set());
  const notify = (message) => { setToast(message); window.setTimeout(() => setToast(""), 3200); };

  useEffect(() => {
    api("/api/provider/status").then(setProvider).catch((error) => setFatal(error.message));
    const implementationId = new URLSearchParams(window.location.search).get("implementation");
    if (implementationId) {
      setBusy("implementation-load");
      api(`/api/implementations/${encodeURIComponent(implementationId)}`)
        .then(setImplementation)
        .catch((error) => setFatal(error.message))
        .finally(() => setBusy(""));
    }
  }, []);
  useEffect(() => {
    if (session?.definition?.steps?.length && !session.definition.steps.some((step) => step.target === selectedField)) {
      setSelectedField(session.definition.steps[0].target);
    }
  }, [session, selectedField]);
  useEffect(() => {
    const eligible = new Set(session?.definition?.steps?.filter((step) => step.op !== "unmapped" && !step.review?.locked).map((step) => step.target) || []);
    setBulkSelectedFields((current) => current.filter((field) => eligible.has(field)));
  }, [session?.active_table_mapping_id, session?.revision]);
  useEffect(() => {
    if (!session || busy) return;
    if (session.stage === "table_mapping" && !session.table_mapping_proposal.length) {
      const key = `table:${session.session_id}`;
      if (!autoRequests.current.has(key)) {
        autoRequests.current.add(key);
        void proposeTables("", true);
      }
      return;
    }
    const needsFields = session.stage === "field_mapping" && session.definition?.steps?.every((step) => step.op === "unmapped");
    if (needsFields) {
      const key = `field:${session.session_id}:${session.active_table_mapping_id}`;
      if (!autoRequests.current.has(key)) {
        autoRequests.current.add(key);
        void generateFields(true);
      }
    }
  }, [session, busy]);

  const createSession = async (sourceFile, targetFile) => {
    setBusy("upload");
    setAiError(null);
    try {
      const [source_file, target_file] = await Promise.all([encodeFile(sourceFile), encodeFile(targetFile)]);
      const next = await api("/api/sessions", { method: "POST", body: JSON.stringify({ source_file, target_file }) });
      setSession(next); setProposal(null); setImplementation(null); setBulkSelectedFields([]); notify("Both documents were read and their table regions were detected.");
    } catch (error) { notify(error.message); } finally { setBusy(""); }
  };
  const proposeTables = async (commentText = "", automatic = false) => {
    setBusy("table-ai");
    setAiError(null);
    try {
      const result = await api(`/api/sessions/${session.session_id}/table-proposal`, { method: "POST", body: JSON.stringify({ comment: commentText.trim() }) });
      setSession(result.session);
      if (!automatic) notify(result.message);
    }
    catch (error) { setAiError({ scope: "table", message: error.message }); notify(error.message); } finally { setBusy(""); }
  };
  const confirmTables = async (selections) => {
    setBusy("table-confirm");
    setAiError(null);
    try {
      const mappings = session.target_tables.map((target) => ({ target_table_id: target.id, source_table_id: selections[target.id] }));
      const next = await api(`/api/sessions/${session.session_id}/table-confirm`, { method: "POST", body: JSON.stringify({ mappings }) });
      setSession(next); setSelectedField(next.definition.steps[0]?.target || ""); setBulkSelectedFields([]); notify("Table matches confirmed. Field review is now open.");
    } catch (error) { notify(error.message); } finally { setBusy(""); }
  };
  const activate = async (mappingId) => {
    setBusy("activate");
    setAiError(null);
    try { const next = await api(`/api/sessions/${session.session_id}/table-mappings/${mappingId}/activate`, { method: "POST" }); setSession(next); setSelectedField(next.definition.steps[0]?.target || ""); setBulkSelectedFields([]); setComment(""); setProposal(null); }
    catch (error) { notify(error.message); } finally { setBusy(""); }
  };
  const generateFields = async (automatic = false) => {
    const mappingId = session.active_table_mapping_id;
    setBusy("field-ai");
    setAiError(null);
    try {
      const result = await api(`/api/sessions/${session.session_id}/proposal`, { method: "POST" });
      setSession(result.proposal);
      if (!automatic) notify(result.message);
    }
    catch (error) { setAiError({ scope: "field", mappingId, message: error.message }); notify(error.message); } finally { setBusy(""); }
  };
  const acceptCurrent = async (field) => {
    setBusy("accept-current");
    try { const next = await api(`/api/sessions/${session.session_id}/fields/${encodeURIComponent(field)}/accept`, { method: "POST" }); setSession(next); notify(`${titleCase(field)} accepted and locked.`); }
    catch (error) { notify(error.message); } finally { setBusy(""); }
  };
  const acceptBulk = async (fields, acceptAll) => {
    setBusy("accept-bulk");
    try {
      const result = await api(`/api/sessions/${session.session_id}/fields/accept`, { method: "POST", body: JSON.stringify({ fields, accept_all: acceptAll }) });
      setSession(result.session); setBulkSelectedFields([]); notify(result.message);
    }
    catch (error) { notify(error.message); } finally { setBusy(""); }
  };
  const proposeChange = async (field) => {
    if (!comment.trim()) return notify("Describe the correction first.");
    setBusy("revision");
    try { const result = await api(`/api/sessions/${session.session_id}/revisions`, { method: "POST", body: JSON.stringify({ field, comment: comment.trim(), base_revision: session.revision }) }); setProposal(result); }
    catch (error) { notify(error.message); } finally { setBusy(""); }
  };
  const acceptRevision = async () => {
    setBusy("accept");
    try { const next = await api(`/api/sessions/${session.session_id}/revisions/${proposal.revision_id}/accept`, { method: "POST" }); setSession(next); setProposal(null); notify("Revision accepted and locked."); }
    catch (error) { notify(error.message); } finally { setBusy(""); }
  };
  const validate = async () => {
    setBusy("validate");
    try { await api(`/api/sessions/${session.session_id}/validate`, { method: "POST" }); const next = await api(`/api/sessions/${session.session_id}`); setSession(next); notify(next.validation.blockers ? "Validation found blockers." : "This table pair passed."); }
    catch (error) { notify(error.message); } finally { setBusy(""); }
  };
  const saveImplementation = async () => {
    setBusy("save");
    try {
      const result = await api(`/api/sessions/${session.session_id}/publish`, { method: "POST" });
      setImplementation(result.implementation);
      window.history.replaceState({}, "", `${window.location.pathname}?implementation=${encodeURIComponent(session.session_id)}`);
      notify(result.message);
    }
    catch (error) { notify(error.message); } finally { setBusy(""); }
  };
  const reset = () => { setSession(null); setProposal(null); setImplementation(null); setSelectedField(""); setBulkSelectedFields([]); setComment(""); setAiError(null); autoRequests.current.clear(); window.history.replaceState({}, "", window.location.pathname); };

  if (fatal) return <main className="fatal"><h1>Workbench unavailable</h1><p>{fatal}</p></main>;
  let content;
  if (implementation) content = <ImplementationStage implementation={implementation} onReset={reset} />;
  else if (!session) content = <UploadStage provider={provider} busy={busy} onCreate={createSession} />;
  else if (session.stage === "table_mapping") content = <TableMappingStage session={session} busy={busy} aiError={aiError} onPropose={proposeTables} onConfirm={confirmTables} onReset={reset} />;
  else content = <FieldMappingStage session={session} busy={busy} aiError={aiError} proposal={proposal} selectedField={selectedField} bulkSelectedFields={bulkSelectedFields} setBulkSelectedFields={setBulkSelectedFields} comment={comment} setComment={setComment} setSelectedField={setSelectedField} actions={{ activate, retryFields: () => generateFields(false), acceptCurrent, acceptBulk, proposeChange, acceptRevision, validate, saveImplementation, reset, dismissProposal: () => setProposal(null) }} />;
  return <>{content}{toast && <div className="toast">{toast}</div>}</>;
}

createRoot(document.getElementById("root")).render(<React.StrictMode><App /></React.StrictMode>);
