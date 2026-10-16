import { useEffect, useRef } from 'react';

export default function ShaderBackground() {
  const mountedRef = useRef(false);

  useEffect(() => {
    if (mountedRef.current) return;
    mountedRef.current = true;

    const container = document.getElementById('shader-bg');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const THREE = (window as any).THREE;
    if (!container || !THREE) return;

    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    const scene = new THREE.Scene();
    const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
    const renderer = new THREE.WebGLRenderer({ antialias: false });
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 1);
    renderer.setPixelRatio(pixelRatio);
    renderer.setSize(window.innerWidth, window.innerHeight);
    container.appendChild(renderer.domElement);

    const material = new THREE.ShaderMaterial({
      uniforms: {
        iTime: { value: 0 },
        iResolution: {
          value: new THREE.Vector2(
            window.innerWidth * pixelRatio,
            window.innerHeight * pixelRatio
          ),
        },
      },
      vertexShader: `void main() { gl_Position = vec4(position, 1.0); }`,
      fragmentShader: `
        uniform float iTime;
        uniform vec2 iResolution;
        #define NUM_OCTAVES 3
        float rand(vec2 n) { return fract(sin(dot(n, vec2(12.9898, 4.1414))) * 43758.5453); }
        float noise(vec2 p) {
            vec2 ip = floor(p); vec2 u = fract(p);
            u = u*u*(3.0-2.0*u);
            float res = mix(mix(rand(ip), rand(ip+vec2(1,0)), u.x), mix(rand(ip+vec2(0,1)), rand(ip+vec2(1,1)), u.x), u.y);
            return res*res;
        }
        float fbm(vec2 x) {
            float v=0.0; float a=0.3; vec2 shift=vec2(100);
            mat2 rot=mat2(cos(0.5),sin(0.5),-sin(0.5),cos(0.5));
            for(int i=0;i<NUM_OCTAVES;++i){ v+=a*noise(x); x=rot*x*2.0+shift; a*=0.4; }
            return v;
        }
        void main() {
            vec2 shake = vec2(sin(iTime*1.2)*0.005, cos(iTime*2.1)*0.005);
            vec2 p = ((gl_FragCoord.xy + shake*iResolution.xy) - iResolution.xy*0.5) / iResolution.y * mat2(6.0,-4.0,4.0,6.0);
            float t = iTime * 0.12;
            vec2 q = vec2(fbm(p+vec2(0,0)), fbm(p+vec2(5.2,1.3)));
            vec2 r = vec2(fbm(p+4.0*q+vec2(1.7+t,9.2)), fbm(p+4.0*q+vec2(8.3+t,2.8)));
            float f = fbm(p + 4.0*r);
            vec3 col = mix(vec3(0.01,0.04,0.02), vec3(0.02,0.1,0.05), clamp(f*2.0,0.0,1.0));
            col = mix(col, vec3(0.0,0.18,0.08), clamp(length(q)*1.5,0.0,1.0));
            col = mix(col, vec3(0.06,0.24,0.12), clamp(f*f*4.0,0.0,1.0));
            col *= 0.5 + 0.5*f;
            col = mix(col, vec3(0.01,0.04,0.02), clamp(dot(p,p)*0.04,0.0,1.0));
            gl_FragColor = vec4(col, 1.0);
        }
      `,
    });

    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), material);
    scene.add(mesh);

    let animId: number;
    const clock = new THREE.Clock();

    const animate = () => {
      animId = requestAnimationFrame(animate);
      if (!reduceMotion) {
        material.uniforms.iTime.value = clock.getElapsedTime();
      }
      renderer.render(scene, camera);
    };
    animate();

    const onResize = () => {
      renderer.setSize(window.innerWidth, window.innerHeight);
      material.uniforms.iResolution.value.set(
        window.innerWidth * pixelRatio,
        window.innerHeight * pixelRatio
      );
    };
    window.addEventListener('resize', onResize);

    return () => {
      cancelAnimationFrame(animId);
      window.removeEventListener('resize', onResize);
      renderer.dispose();
      if (container.contains(renderer.domElement)) {
        container.removeChild(renderer.domElement);
      }
    };
  }, []);

  return <div id="shader-bg" aria-hidden="true" />;
}
