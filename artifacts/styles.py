from dataclasses import dataclass,asdict
import re
from materials.types import MaterialError

@dataclass(frozen=True)
class StyleProfile:
    background: str='#F4F3ED'
    foreground: str='#192A35'
    accent: str='#176C78'
    muted: str='#445660'
    density: str='comfortable'
    illustration_tone: str='calm'
    font: str='DejaVuSans'
    def __post_init__(self):
        if any(not re.fullmatch(r'#[0-9A-Fa-f]{6}',c) for c in (self.background,self.foreground,self.accent,self.muted)) or self.density not in ('comfortable','compact') or self.illustration_tone not in ('calm','playful','formal') or self.font!='DejaVuSans': raise MaterialError('artifact_style_invalid')
        def lum(c):
            vals=[int(c[i:i+2],16)/255 for i in (1,3,5)]; vals=[x/12.92 if x<=.04045 else ((x+.055)/1.055)**2.4 for x in vals]
            return sum(a*b for a,b in zip(vals,(.2126,.7152,.0722)))
        b=lum(self.background)
        for c in (self.foreground,self.accent,self.muted):
            f=lum(c)
            if (max(b,f)+.05)/(min(b,f)+.05)<4.5: raise MaterialError('artifact_contrast')
    def to_dict(self): return asdict(self)
    @classmethod
    def from_dict(cls,v):
        try: return cls(**v)
        except TypeError: raise MaterialError('artifact_style_invalid') from None

STYLES={'calm':StyleProfile(),'ink':StyleProfile('#FFFFFF','#182332','#374D73','#485769','compact','formal'),'night':StyleProfile('#172735','#FAF8ED','#90DADB','#BDC6CB')}
