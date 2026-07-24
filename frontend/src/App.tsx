import { useState, useEffect } from "react";
import { applyMode, Mode } from "@cloudscape-design/global-styles";
import AppLayout from "@cloudscape-design/components/app-layout";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Header from "@cloudscape-design/components/header";
import Toggle from "@cloudscape-design/components/toggle";
import SideNavigation, { SideNavigationProps } from "@cloudscape-design/components/side-navigation";
import ReplicatorWizard from "./components/Wizard/ReplicatorWizard";
import SessionsListPage from "./components/Session/SessionsListPage";
import "./failover-link.css";
import SessionStatusPage from "./components/Session/SessionStatusPage";
import QuotaComparison from "./components/Quota/QuotaComparison";
import DiscoverAssociate from "./components/Discover/DiscoverAssociate";
import ContactFlowAnalysis from "./components/ContactFlows/ContactFlowAnalysis";

const THEME_KEY = "acgr-replicator-dark-mode";

function getInitialDark(): boolean {
  const stored = localStorage.getItem(THEME_KEY);
  if (stored !== null) return stored === "true";
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

type Route =
  | { page: "wizard" }
  | { page: "sessions" }
  | { page: "session"; sessionId: string }
  | { page: "quotas" }
  | { page: "discover" }
  | { page: "contact-flows" };

function parseHash(): Route {
  const hash = window.location.hash;
  const sessionMatch = hash.match(/^#\/session\/(.+)$/);
  if (sessionMatch) return { page: "session", sessionId: decodeURIComponent(sessionMatch[1]) };
  if (hash === "#/sessions") return { page: "sessions" };
  if (hash === "#/quotas") return { page: "quotas" };
  if (hash === "#/discover") return { page: "discover" };
  if (hash === "#/contact-flows") return { page: "contact-flows" };
  return { page: "wizard" };
}

export default function App() {
  const [dark, setDark] = useState(getInitialDark);
  const [route, setRoute] = useState<Route>(parseHash);

  useEffect(() => {
    applyMode(dark ? Mode.Dark : Mode.Light);
    localStorage.setItem(THEME_KEY, String(dark));
  }, [dark]);

  useEffect(() => {
    const onHashChange = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const navItems: SideNavigationProps.Item[] = [
    { type: "link", text: "Wizard", href: "#/" },
    { type: "link", text: "Sessions", href: "#/sessions" },
    { type: "link", text: "Contact Flows", href: "#/contact-flows" },
    { type: "link", text: "Quota Comparison", href: "#/quotas" },
    { type: "link", text: "Discover & Associate", href: "#/discover" },
  ];

  const activeHref =
    route.page === "sessions" ? "#/sessions" :
    route.page === "session" ? "#/sessions" :
    route.page === "quotas" ? "#/quotas" :
    route.page === "discover" ? "#/discover" :
    route.page === "contact-flows" ? "#/contact-flows" :
    "#/";

  return (
    <AppLayout
      navigation={
        <>
          <SideNavigation
            header={{ text: "ACGR Replicator", href: "#/" }}
            items={navItems}
            activeHref={activeHref}
          />
          <a
            className="acgr-failover-link"
            href="https://d2s6piob7oyqjz.cloudfront.net/#/agent-associations"
            target="_blank"
            rel="noopener noreferrer"
          >
            <span className="acgr-failover-icon" aria-hidden="true">&#8599;</span>
            Failover Tool
          </a>
        </>
      }
      toolsHide
      content={
        <ContentLayout
          header={
            <Header
              variant="h1"
              description="Discover and replicate AWS resources for Amazon Connect Global Resiliency"
              actions={
                <Toggle
                  checked={dark}
                  onChange={({ detail }) => setDark(detail.checked)}
                >
                  Dark mode
                </Toggle>
              }
            >
              Connect ACGR Resource Replicator
            </Header>
          }
        >
          {route.page === "sessions" ? (
            <SessionsListPage />
          ) : route.page === "session" ? (
            <SessionStatusPage initialSessionId={route.sessionId} />
          ) : route.page === "quotas" ? (
            <QuotaComparison />
          ) : route.page === "discover" ? (
            <DiscoverAssociate />
          ) : route.page === "contact-flows" ? (
            <ContactFlowAnalysis />
          ) : (
            <ReplicatorWizard />
          )}
        </ContentLayout>
      }
    />
  );
}
