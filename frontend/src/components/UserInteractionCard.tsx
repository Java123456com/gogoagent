import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { UserInteraction } from '../types';

/** Markdown 渲染的 question 区域 */
function QuestionBlock({ text }: { text: string }) {
  return (
    <div className="interaction-question markdown-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
        a: ({ href, children, ...props }) => (
          <a href={href} target="_blank" rel="noopener noreferrer" {...props}>{children}</a>
        ),
      }}>{text}</ReactMarkdown>
    </div>
  );
}

interface Props {
  interaction: UserInteraction;
  onSubmit: (value: unknown) => void;
}

export default function UserInteractionCard({ interaction, onSubmit }: Props) {
  const { question, ui_type, options, fields, default_value, allow_other } = interaction;

  switch (ui_type) {
    case 'select':
      return <SelectInteraction question={question} options={options} allowOther={allow_other} onSubmit={onSubmit} />;
    case 'multi_select':
      return <MultiSelectInteraction question={question} options={options} allowOther={allow_other} onSubmit={onSubmit} />;
    case 'confirm':
      return <ConfirmInteraction question={question} onSubmit={onSubmit} />;
    case 'tool_confirmation':
      return <ToolConfirmationInteraction question={question} onSubmit={onSubmit} />;
    case 'form':
      return <FormInteraction question={question} fields={fields} onSubmit={onSubmit} />;
    case 'date':
      return <InputInteraction question={question} type="date" defaultValue={default_value} onSubmit={onSubmit} />;
    case 'number':
      return <InputInteraction question={question} type="number" defaultValue={default_value} onSubmit={onSubmit} />;
    case 'text':
    default:
      return <InputInteraction question={question} type="textarea" defaultValue={default_value} onSubmit={onSubmit} />;
  }
}

function SelectInteraction({
  question,
  options,
  allowOther,
  onSubmit,
}: {
  question: string;
  options?: string[];
  allowOther?: boolean;
  onSubmit: (value: unknown) => void;
}) {
  const [selected, setSelected] = useState<string>('');
  const [other, setOther] = useState('');

  const handleSubmit = () => {
    const value = selected === '__other__' ? other : selected;
    onSubmit(value);
  };

  return (
    <div className="interaction-card">
      <QuestionBlock text={question} />
      <div className="interaction-options">
        {(options || []).map((opt) => (
          <label key={opt} className="interaction-option">
            <input
              type="radio"
              name="select"
              value={opt}
              checked={selected === opt}
              onChange={() => setSelected(opt)}
            />
            <span>{opt}</span>
          </label>
        ))}
        {allowOther && (
          <label className="interaction-option">
            <input
              type="radio"
              name="select"
              value="__other__"
              checked={selected === '__other__'}
              onChange={() => setSelected('__other__')}
            />
            <span>其他</span>
          </label>
        )}
      </div>
      {selected === '__other__' && (
        <input
          className="interaction-input"
          type="text"
          placeholder="请输入其他内容"
          value={other}
          onChange={(e) => setOther(e.target.value)}
        />
      )}
      <button className="interaction-submit" onClick={handleSubmit} disabled={!selected || (selected === '__other__' && !other)}>
        确认
      </button>
    </div>
  );
}

function MultiSelectInteraction({
  question,
  options,
  allowOther,
  onSubmit,
}: {
  question: string;
  options?: string[];
  allowOther?: boolean;
  onSubmit: (value: unknown) => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [other, setOther] = useState('');
  const [otherEnabled, setOtherEnabled] = useState(false);

  const toggle = (opt: string) => {
    const next = new Set(selected);
    if (next.has(opt)) next.delete(opt);
    else next.add(opt);
    setSelected(next);
  };

  const handleSubmit = () => {
    const values = Array.from(selected);
    if (otherEnabled && other) values.push(other);
    onSubmit(values);
  };

  return (
    <div className="interaction-card">
      <QuestionBlock text={question} />
      <div className="interaction-options">
        {(options || []).map((opt) => (
          <label key={opt} className="interaction-option">
            <input type="checkbox" checked={selected.has(opt)} onChange={() => toggle(opt)} />
            <span>{opt}</span>
          </label>
        ))}
        {allowOther && (
          <label className="interaction-option">
            <input type="checkbox" checked={otherEnabled} onChange={(e) => setOtherEnabled(e.target.checked)} />
            <span>其他</span>
          </label>
        )}
      </div>
      {otherEnabled && (
        <input
          className="interaction-input"
          type="text"
          placeholder="请输入其他内容"
          value={other}
          onChange={(e) => setOther(e.target.value)}
        />
      )}
      <button className="interaction-submit" onClick={handleSubmit} disabled={selected.size === 0 && !(otherEnabled && other)}>
        确认
      </button>
    </div>
  );
}

function ConfirmInteraction({ question, onSubmit }: { question: string; onSubmit: (value: unknown) => void }) {
  return (
    <div className="interaction-card">
      <QuestionBlock text={question} />
      <div className="interaction-actions">
        <button className="interaction-btn secondary" onClick={() => onSubmit('no')}>
          否
        </button>
        <button className="interaction-btn" onClick={() => onSubmit('yes')}>
          是
        </button>
      </div>
    </div>
  );
}

function ToolConfirmationInteraction({
  question,
  onSubmit,
}: {
  question: string;
  onSubmit: (value: unknown) => void;
}) {
  return (
    <div className="interaction-card">
      <QuestionBlock text={question} />
      <div className="interaction-actions">
        <button className="interaction-btn secondary" onClick={() => onSubmit('reject')}>
          取消
        </button>
        <button className="interaction-btn" onClick={() => onSubmit('approve')}>
          确认执行
        </button>
      </div>
    </div>
  );
}

function InputInteraction({
  question,
  type,
  defaultValue,
  onSubmit,
}: {
  question: string;
  type: 'textarea' | 'text' | 'number' | 'date';
  defaultValue?: any;
  onSubmit: (value: unknown) => void;
}) {
  const [value, setValue] = useState(defaultValue ?? '');

  const handleSubmit = () => {
    if (type === 'number') {
      onSubmit(Number(value));
    } else {
      onSubmit(value);
    }
  };

  return (
    <div className="interaction-card">
      <QuestionBlock text={question} />
      {type === 'textarea' ? (
        <textarea
          className="interaction-textarea"
          rows={3}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              handleSubmit();
            }
          }}
        />
      ) : (
        <input
          className="interaction-input"
          type={type}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              handleSubmit();
            }
          }}
        />
      )}
      <button className="interaction-submit" onClick={handleSubmit} disabled={!value}>
        确认
      </button>
    </div>
  );
}

function FormInteraction({
  question,
  fields,
  onSubmit,
}: {
  question: string;
  fields?: UserInteraction['fields'];
  onSubmit: (value: unknown) => void;
}) {
  const [values, setValues] = useState<Record<string, any>>({});

  const setField = (name: string, value: any) => {
    setValues((prev) => ({ ...prev, [name]: value }));
  };

  const handleSubmit = () => {
    onSubmit(values);
  };

  const isComplete = (fields || []).every((f) => !f.required || values[f.name] !== undefined && values[f.name] !== '');

  return (
    <div className="interaction-card">
      <QuestionBlock text={question} />
      <div className="interaction-form">
        {(fields || []).map((field) => (
          <div key={field.name} className="interaction-field">
            <label className="interaction-label">
              {field.label}
              {field.required && <span className="interaction-required">*</span>}
            </label>
            {field.type === 'textarea' ? (
              <textarea
                className="interaction-textarea"
                rows={2}
                placeholder={field.placeholder}
                value={values[field.name] || ''}
                onChange={(e) => setField(field.name, e.target.value)}
              />
            ) : field.type === 'select' ? (
              <select
                className="interaction-input"
                value={values[field.name] || ''}
                onChange={(e) => setField(field.name, e.target.value)}
              >
                <option value="">请选择</option>
                {(field.options || []).map((opt) => (
                  <option key={opt} value={opt}>
                    {opt}
                  </option>
                ))}
              </select>
            ) : (
              <input
                className="interaction-input"
                type={field.type}
                placeholder={field.placeholder}
                min={field.min}
                max={field.max}
                step={field.step}
                value={values[field.name] || ''}
                onChange={(e) => setField(field.name, e.target.value)}
              />
            )}
          </div>
        ))}
      </div>
      <button className="interaction-submit" onClick={handleSubmit} disabled={!isComplete}>
        提交
      </button>
    </div>
  );
}
