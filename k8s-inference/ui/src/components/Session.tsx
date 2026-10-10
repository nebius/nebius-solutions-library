import { createContext, useContext, type ReactNode } from "react";
import { api } from "../api/client";
import type { ApiKey, FleetInfo } from "../api/types";
import { useAsync } from "./ui";

type SessionData = { key: ApiKey | null; fleet: FleetInfo | null };
type SessionState = SessionData & {
  admin: boolean;
  error: string | null;
  reload: () => void;
};
const Session = createContext<SessionState>({
  key: null,
  fleet: null,
  admin: false,
  error: null,
  reload: () => {},
});
export const useSession = () => useContext(Session);
export const useAdmin = () => useSession().admin;

export function SessionProvider({
  children,
  value,
}: {
  children: ReactNode;
  value?: SessionData;
}) {
  if (value)
    return (
      <Session.Provider
        value={{
          ...value,
          admin: value.key?.role === "admin",
          error: null,
          reload: () => {},
        }}
      >
        {children}
      </Session.Provider>
    );
  return <ConnectedSession>{children}</ConnectedSession>;
}
function ConnectedSession({ children }: { children: ReactNode }) {
  const key = useAsync(() => api().keyInfo(), []);
  const fleet = useAsync(() => api().fleetInfo(), []);
  return (
    <Session.Provider
      value={{
        key: key.data,
        fleet: fleet.data,
        admin: key.data?.role === "admin",
        error: key.error,
        reload: key.reload,
      }}
    >
      {children}
    </Session.Provider>
  );
}
