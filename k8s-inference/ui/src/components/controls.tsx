import type { ComponentProps, ReactNode } from "react";
import {
  Button as GravityButton,
  Icon,
  Select,
  Tab,
  TabList,
  TextArea,
  TextInput,
} from "@gravity-ui/uikit";
import { Magnifier } from "@gravity-ui/icons";

// Thin adapters keep one size and spacing convention throughout the standalone console.
export function Button({
  className = "",
  primary,
  danger,
  ...props
}: ComponentProps<typeof GravityButton> & {
  primary?: boolean;
  danger?: boolean;
}) {
  const view =
    props.view ??
    (danger ? "outlined-danger" : primary ? "action" : "outlined");
  return (
    <GravityButton
      {...props}
      view={view}
      size={props.size ?? "l"}
      className={className}
    />
  );
}

type ControlA11y = { "aria-describedby"?: string; "aria-invalid"?: boolean };
export function Input({
  value,
  onChange,
  mono,
  "aria-describedby": describedBy,
  "aria-invalid": invalid,
  ...props
}: Omit<ComponentProps<typeof TextInput>, "onChange" | "value"> &
  ControlA11y & {
    value: string | number;
    onChange?: (value: string) => void;
    mono?: boolean;
  }) {
  return (
    <TextInput
      {...props}
      controlProps={{
        ...props.controlProps,
        "aria-describedby":
          describedBy ?? props.controlProps?.["aria-describedby"],
      }}
      validationState={invalid ? "invalid" : props.validationState}
      size="l"
      value={String(value)}
      onUpdate={onChange}
      className={[props.className, mono ? "mono-control" : ""]
        .filter(Boolean)
        .join(" ")}
    />
  );
}
export function Area({
  onChange,
  mono = true,
  "aria-describedby": describedBy,
  "aria-invalid": invalid,
  ...props
}: Omit<ComponentProps<typeof TextArea>, "onChange"> &
  ControlA11y & { onChange?: (value: string) => void; mono?: boolean }) {
  return (
    <TextArea
      {...props}
      controlProps={{
        ...props.controlProps,
        "aria-describedby":
          describedBy ?? props.controlProps?.["aria-describedby"],
      }}
      validationState={invalid ? "invalid" : props.validationState}
      size="l"
      onUpdate={onChange}
      className={mono ? "mono-control" : ""}
    />
  );
}
export type Choice =
  | string
  | { value: string; label: string; disabled?: boolean };
export function Pick({
  value,
  onChange,
  options,
  ...props
}: Omit<ComponentProps<typeof Select>, "value" | "onUpdate" | "options"> & {
  value: string;
  onChange: (value: string) => void;
  options: Choice[];
}) {
  return (
    <Select
      {...props}
      size="l"
      width="max"
      value={value ? [value] : []}
      onUpdate={(v) => onChange(v[0] ?? "")}
      options={options.map((o) =>
        typeof o === "string"
          ? { value: o, content: o }
          : { ...o, content: o.label },
      )}
    />
  );
}
export function MultiPick({
  options,
  ...props
}: Omit<ComponentProps<typeof Select>, "options"> & { options: Choice[] }) {
  return (
    <Select
      {...props}
      size="l"
      width="max"
      multiple
      filterable
      hasClear
      options={options.map((o) =>
        typeof o === "string"
          ? { value: o, content: o }
          : { ...o, content: o.label },
      )}
    />
  );
}
export function Search({
  value,
  onChange,
  label = "Search",
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  label?: string;
  placeholder?: string;
}) {
  return (
    <Input
      value={value}
      onChange={onChange}
      type="search"
      hasClear
      placeholder={placeholder ?? label}
      controlProps={{ "aria-label": label }}
      startContent={<Icon data={Magnifier} size={16} />}
    />
  );
}
export function PageHeader({
  title,
  description,
  count,
  children,
}: {
  title: ReactNode;
  description?: ReactNode;
  count?: number;
  children?: ReactNode;
}) {
  return (
    <div className="page-head">
      <div>
        <div className="page-title">
          <h1>{title}</h1>
          {count != null && <span className="resource-count">{count}</span>}
        </div>
        {description && <p>{description}</p>}
      </div>
      {children && <div className="actions">{children}</div>}
    </div>
  );
}
export function FormSection({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section className="form-section">
      <div>
        <h2>{title}</h2>
        {description && <p>{description}</p>}
      </div>
      <div className="form-section__fields">{children}</div>
    </section>
  );
}
export function Tabs({
  activeTab,
  onSelectTab,
  items,
  ...props
}: {
  activeTab: string;
  onSelectTab: (value: string) => void;
  items: { id: string; title: string }[];
} & Omit<ComponentProps<typeof TabList>, "children" | "value" | "onUpdate">) {
  return (
    <TabList {...props} value={activeTab} onUpdate={onSelectTab} size="l">
      {items.map((item) => (
        <Tab key={item.id} value={item.id}>
          {item.title}
        </Tab>
      ))}
    </TabList>
  );
}
