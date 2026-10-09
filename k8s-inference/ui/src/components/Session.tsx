import { createContext, useContext, type ReactNode } from "react";
import type { ApiKey, FleetInfo } from "../api/types";
export type Session = { key: ApiKey | null; fleet: FleetInfo | null };
const Context = createContext<Session>({ key: null, fleet: null });
export function SessionProvider({
  value,
  children,
}: {
  value: Session;
  children: ReactNode;
}) {
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
export function useSession() {
  return useContext(Context);
}
export function useAdmin() {
  return useSession().key?.role === "admin";
}
