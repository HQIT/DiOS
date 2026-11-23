import { t as translateUI } from "../lib/i18n";
import type { ReactNode } from "react";

interface Props {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
}

export default function Drawer({ open, title, onClose, children }: Props) {
  if (!open) return null;

  return (
    <>
      <div className="drawer-overlay" onClick={onClose} />
      <div className="drawer">
        <div className="drawer-header">
          <h3>{translateUI(title)}</h3>
          <button className="btn-icon" onClick={onClose}>&times;</button>
        </div>
        <div className="drawer-body">{translateUI(children)}</div>
      </div>
    </>
  );
}
