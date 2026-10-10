// Tiny hash router: #/models, #/jobs/op-123, #/jobs/new?model=container-run ...
import { useEffect, useState } from "react";

export interface Route {
  path: string;
  parts: string[];
  query: URLSearchParams;
}
function parse(): Route {
  const h = location.hash.replace(/^#\/?/, "");
  const [p, q = ""] = h.split("?");
  const parts = p.split("/").filter(Boolean);
  return { path: "/" + parts.join("/"), parts, query: new URLSearchParams(q) };
}
export function useRoute(): Route {
  const [r, set] = useState(parse);
  useEffect(() => {
    const f = () => set(parse());
    addEventListener("hashchange", f);
    return () => removeEventListener("hashchange", f);
  }, []);
  return r;
}
export function navigate(to: string) {
  location.hash = to.startsWith("#") ? to : "#" + to;
}
export function href(to: string) {
  return "#" + to;
}
