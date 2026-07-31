import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "企业招聘官网 ATS 检测",
  description: "独立的企业招聘官网 ATS 检测前端原型。",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
