import { useEffect, useId, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeKatex from 'rehype-katex';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';

type MermaidApi = {
  initialize: (options: Record<string, unknown>) => void;
  render: (id: string, source: string) => Promise<{ svg: string }>;
};

let mermaidLoader: Promise<MermaidApi> | null = null;

function loadMermaid(): Promise<MermaidApi> {
  if (mermaidLoader) return mermaidLoader;
  mermaidLoader = new Promise((resolve, reject) => {
    const existing = (window as unknown as { mermaid?: MermaidApi }).mermaid;
    if (existing) return resolve(existing);
    const script = document.createElement('script');
    script.src = '/mermaid.min.js';
    script.async = true;
    script.onload = () => {
      const api = (window as unknown as { mermaid?: MermaidApi }).mermaid;
      if (api) resolve(api);
      else reject(new Error('Mermaid did not initialize.'));
    };
    script.onerror = () => reject(new Error('Could not load Mermaid.'));
    document.head.appendChild(script);
  });
  return mermaidLoader;
}

function MermaidDiagram({ source }: { source: string }) {
  const id = `mermaid-${useId().replace(/:/g, '')}`;
  const [svg, setSvg] = useState('');
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    loadMermaid().then(async (mermaid) => {
      mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'strict' });
      try {
        const rendered = await mermaid.render(id, source);
        if (active) setSvg(rendered.svg);
      } catch {
        if (active) setFailed(true);
      }
    });
    return () => { active = false; };
  }, [id, source]);

  if (failed) return <pre className="vanta-code">{source}</pre>;
  return <div className="vanta-diagram" dangerouslySetInnerHTML={{ __html: svg }} />;
}

export function ReportCard({ content }: { content: string }) {
  return (
    <div className="vanta-report">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={{
          code({ className, children, ...props }) {
            const language = /language-(\w+)/.exec(className || '')?.[1];
            const source = String(children).replace(/\n$/, '');
            if (language === 'mermaid') return <MermaidDiagram source={source} />;
            return <code className={className} {...props}>{children}</code>;
          },
          a({ children, ...props }) {
            return <a {...props} target="_blank" rel="noreferrer">{children}</a>;
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
