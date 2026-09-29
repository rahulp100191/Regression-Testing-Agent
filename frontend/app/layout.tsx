import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Workday Release Regression Agent",
  description: "Map Workday release changes to E2E regression cases with grounded reasoning.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}

