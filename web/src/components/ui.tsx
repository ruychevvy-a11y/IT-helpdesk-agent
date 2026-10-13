import type { ReactNode } from "react";
import { TONE_CLASSES, type Tone } from "../labels";

export function Badge({ tone = "slate", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded-md px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset ${TONE_CLASSES[tone]}`}
    >
      {children}
    </span>
  );
}

export function Button({
  children,
  onClick,
  variant = "primary",
  disabled,
  type = "button",
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "secondary" | "danger";
  disabled?: boolean;
  type?: "button" | "submit";
}) {
  const styles = {
    primary: "bg-indigo-600 text-white hover:bg-indigo-500",
    secondary: "bg-white text-slate-700 ring-1 ring-inset ring-slate-300 hover:bg-slate-50",
    danger: "bg-rose-600 text-white hover:bg-rose-500",
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`rounded-lg px-3 py-1.5 text-sm font-medium shadow-sm transition disabled:cursor-not-allowed disabled:opacity-50 ${styles[variant]}`}
    >
      {children}
    </button>
  );
}

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className="w-full rounded-lg border-0 px-3 py-2 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 placeholder:text-slate-400 focus:ring-2 focus:ring-inset focus:ring-indigo-600 focus:outline-none"
    />
  );
}

export function Modal({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
      <div className="w-full max-w-md rounded-2xl bg-white p-6 shadow-xl">
        <h2 className="mb-4 text-base font-semibold text-slate-900">{title}</h2>
        {children}
      </div>
    </div>
  );
}

// 回答里的《标题》显示成引用标签
export function RichText({ text }: { text: string }) {
  const parts = text.split(/(《[^》]+》)/);
  return (
    <span className="whitespace-pre-wrap">
      {parts.map((part, index) =>
        part.startsWith("《") ? (
          <span
            key={index}
            className="mx-0.5 rounded bg-indigo-50 px-1 text-indigo-700 ring-1 ring-indigo-200"
          >
            {part}
          </span>
        ) : (
          part
        ),
      )}
    </span>
  );
}
