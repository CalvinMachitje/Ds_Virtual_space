// Entry point of the React application
// Client/src/main.tsx
import { createRoot } from "react-dom/client";
import App from "./App.tsx";
import "./index.css";
import React from 'react';
import { AuthProvider } from "./context/AuthContext.tsx";

createRoot(document.getElementById("root")!).render(
    //<React.StrictMode>
    <AuthProvider>
      <App />
    </AuthProvider>
  //</React.StrictMode>
);