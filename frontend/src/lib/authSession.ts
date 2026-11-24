import { createContext } from "react";

export const AuthSessionContext = createContext<{
  browser: boolean;
  principal: boolean;
  label: string;
  signOut?: () => Promise<void>;
}>({ browser: false, principal: false, label: "" });
