export const ATS_PROVIDERS = [
  "greenhouse",
  "lever",
  "ashby",
  "beisen",
  "smartrecruiters",
  "workable",
] as const;
export type AtsProvider = (typeof ATS_PROVIDERS)[number];
export const ATS_PROVIDER_SET: ReadonlySet<string> = new Set(ATS_PROVIDERS);

const GREENHOUSE_HOSTS = new Set([
  "boards.greenhouse.io",
  "boards.eu.greenhouse.io",
  "job-boards.greenhouse.io",
]);
const LEVER_HOSTS = new Set(["jobs.lever.co", "jobs.eu.lever.co"]);
const ASHBY_HOSTS = new Set(["jobs.ashbyhq.com"]);
const SMARTRECRUITERS_HOSTS = new Set([
  "careers.smartrecruiters.com",
  "jobs.smartrecruiters.com",
]);
const TENANT_PATTERN = /^[A-Za-z0-9_-]{1,100}$/;
const DNS_LABEL_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

export function isCollectableCareerUrl(ats: string, url: string): boolean {
  if (!ATS_PROVIDER_SET.has(ats)) {
    return false;
  }
  try {
    const rawAuthority = /^[a-z][a-z0-9+.-]*:\/\/([^/?#]*)/i.exec(url)?.[1];
    if (!rawAuthority || !/^[A-Za-z0-9.:-]+$/.test(rawAuthority)) {
      return false;
    }
    const parsed = new URL(url);
    if (
      parsed.protocol !== "https:" ||
      parsed.username ||
      parsed.password ||
      (parsed.port && parsed.port !== "443")
    ) {
      return false;
    }
    const hostname = parsed.hostname.toLowerCase();
    if (ats === "beisen") {
      if (!hostname.endsWith(".zhiye.com") || hostname.length > 253) {
        return false;
      }
      const tenantLabels = hostname
        .slice(0, -".zhiye.com".length)
        .split(".");
      return (
        tenantLabels.length > 0 &&
        tenantLabels.every((label) => DNS_LABEL_PATTERN.test(label))
      );
    }
    const tenant = parsed.pathname.replace(/^\/+|\/+$/g, "").split("/", 1)[0];
    if (ats === "workable") {
      return (
        hostname === "apply.workable.com" &&
        tenant.toLowerCase() !== "j" &&
        TENANT_PATTERN.test(tenant)
      );
    }
    const supportedHosts =
      ats === "greenhouse"
        ? GREENHOUSE_HOSTS
        : ats === "lever"
          ? LEVER_HOSTS
          : ats === "ashby"
            ? ASHBY_HOSTS
            : SMARTRECRUITERS_HOSTS;
    return supportedHosts.has(hostname) && TENANT_PATTERN.test(tenant);
  } catch {
    return false;
  }
}
