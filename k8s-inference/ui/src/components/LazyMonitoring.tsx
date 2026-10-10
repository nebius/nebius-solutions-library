import { lazy, Suspense, type ComponentProps } from "react";
import { Loading } from "./ui";
const Monitoring = lazy(() => import("./Monitoring"));
export function ResourceMonitoring(props: ComponentProps<typeof Monitoring>) {
  return (
    <Suspense fallback={<Loading />}>
      <Monitoring {...props} />
    </Suspense>
  );
}
