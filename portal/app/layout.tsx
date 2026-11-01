import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import BrowserNotifier from "@/components/BrowserNotifier";
import ClientErrorReporter from "@/components/ClientErrorReporter";
import Nav, { type NavLink } from "@/components/Nav";
import { loadMe } from "@/lib/me";

export const metadata: Metadata = {
  title: "Lab jobs",
  description: "Campus lab ML job portal",
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const me = await loadMe();
  const links: NavLink[] = [
    { href: "/", label: "Queue" },
    { href: "/submit", label: "Submit" },
  ];
  if (me?.email) {
    links.push(
      { href: "/projects", label: "Projects" },
      { href: "/usage", label: "Usage" },
      { href: "/settings", label: "Settings" },
    );
  }
  const adminLinks: NavLink[] = [];
  if (me?.is_admin) {
    adminLinks.push(
      { href: "/admin", label: "Dashboard" },
      { href: "/admin/nodes", label: "Nodes" },
      { href: "/admin/jobs", label: "Jobs" },
      { href: "/admin/alerts", label: "Alerts" },
      { href: "/admin/usage", label: "Lab usage" },
      { href: "/admin/schedules", label: "Schedules" },
      { href: "/admin/users", label: "Users" },
      { href: "/admin/catalog", label: "Catalog" },
      { href: "/admin/rollout", label: "Rollout" },
      { href: "/admin/audit", label: "Audit" },
    );
  }
  return (
    <html lang="en">
      <body>
        <ClientErrorReporter />
        <a className="skip-link" href="#main">
          Skip to content
        </a>
        <div className="shell">
          <header className="top">
            <Link href="/" className="brand">
              Lab <span>jobs</span>
            </Link>
            <Nav links={links} adminLinks={adminLinks} email={me?.email ?? null} />
          </header>
          <main id="main">{children}</main>
          {me?.email ? <BrowserNotifier /> : null}
        </div>
      </body>
    </html>
  );
}
