import { useLang } from '../context/LangContext';

const GithubSVG = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z" />
  </svg>
);

export default function Footer() {
  const { t } = useLang();

  return (
    <footer
      id="contact"
      className="mt-16 py-8"
      style={{
        background: '#040a06',
        borderTop: '1px solid var(--primary-accent)',
      }}
    >
      <div className="container footer-content text-center max-w-3xl mx-auto px-8">
        <h3 className="text-xl font-bold text-white mb-2">{t.footer_title}</h3>
        <p className="text-sm mb-8" style={{ color: 'var(--text-muted)' }}>
          {t.footer_sub}
        </p>

        <div className="footer-links flex justify-center gap-4 mb-8 flex-wrap">
          <a
            href="https://github.com/RealRaKaN"
            target="_blank"
            rel="noopener noreferrer"
            className="footer-btn flex items-center gap-2 px-4 py-2 rounded-lg text-sm transition-all duration-200"
          >
            <GithubSVG />
            <span>{t.foot_github}</span>
          </a>

          <a
            href="https://wa.me/966505180962"
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Contact us on WhatsApp"
            className="footer-btn flex items-center gap-2 px-4 py-2 rounded-lg text-sm transition-all duration-200"
          >
            <img
              src={`${import.meta.env.BASE_URL}assets/images/tools/whatsapp-svgrepo-com.svg`}
              width="20"
              height="20"
              alt="WhatsApp"
            />
            <span>{t.foot_whatsapp}</span>
          </a>
        </div>

        <div
          className="copyright text-xs pt-6"
          style={{
            borderTop: '1px solid #1f2937',
            color: '#6b7280',
          }}
        >
          {t.footer_copy}
        </div>
      </div>
    </footer>
  );
}
