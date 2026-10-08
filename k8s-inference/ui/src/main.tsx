import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { loadConfig } from "./config";
import "./styles/app.css";

loadConfig().then(() => {
  ReactDOM.createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
});
