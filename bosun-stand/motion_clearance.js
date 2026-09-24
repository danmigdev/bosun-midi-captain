/* Conservative contact hints for independent arm and display adjustment.
 * X-axis hinges preserve each part's X span. Clip triangles to overlapping
 * X slabs, then compare convex YZ projections. Concavities are filled, so a
 * positive result means possible contact, not a verified solid intersection.
 * Intended base/arm gear contacts and moving-to-moving contacts are excluded.
 */
function createClearanceChecker(scene) {
    const p = scene.parameters, tolerance = 0.02;
    const releaseLimit = scene.folding?.release_mm || 1.4;
    const method = 'Conservative convex mesh projections; fixed obstacles and floor only. Intended gear contacts and moving-to-moving contacts are excluded.';
    const cross = (o, a, b) => (a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0]);
    function hull(points) {
        const sorted = [...points].sort((a,b)=>a[0]-b[0] || a[1]-b[1]);
        const unique = sorted.filter((v,i)=>!i || Math.abs(v[0]-sorted[i-1][0])>1e-8 || Math.abs(v[1]-sorted[i-1][1])>1e-8);
        if (unique.length < 3) return unique;
        const lower=[], upper=[];
        for (const v of unique) {
            while (lower.length>1 && cross(lower.at(-2),lower.at(-1),v)<=1e-9) lower.pop();
            lower.push(v);
        }
        for (const v of [...unique].reverse()) {
            while (upper.length>1 && cross(upper.at(-2),upper.at(-1),v)<=1e-9) upper.pop();
            upper.push(v);
        }
        lower.pop(); upper.pop(); return lower.concat(upper);
    }
    function clip(polygon, x, sign) {
        const out=[];
        for (let i=0;i<polygon.length;i++) {
            const a=polygon[i], b=polygon[(i+1)%polygon.length];
            const da=sign*(a[0]-x), db=sign*(b[0]-x);
            if (da>=0) out.push(a);
            if ((da>=0)!==(db>=0)) {
                const t=da/(da-db);
                out.push(a.map((v,j)=>v+t*(b[j]-v)));
            }
        }
        return out;
    }
    function project(part, minX, maxX) {
        const points=[];
        for (const face of part.faces) {
            const polygon=clip(clip(face.map(i=>part.vertices[i]),minX,1),maxX,-1);
            for (const v of polygon) points.push([v[1],v[2]]);
        }
        return hull(points);
    }
    function bounds(vertices) {
        return {min:Math.min(...vertices.map(v=>v[0])),max:Math.max(...vertices.map(v=>v[0]))};
    }
    function partLabel(part) {
        const name=part.name;
        if (name.startsWith('Display_')) return 'Display';
        if (name.includes('crossbar')) return 'Display crossbar';
        if (name.includes('spacer')) return 'Display spacer';
        if (name.includes('_arm_')) return name.endsWith('right')?'Right arm':'Left arm';
        if (name.includes('_knob_')) return 'Hand knob';
        if (name.includes('_base_')) return name.endsWith('right')?'Right base':'Left base';
        if (name.includes('_clamp_plate_')) return 'Clamp plate';
        if (name.startsWith('Switch_')) return name.endsWith('_-25')?'Front switches':'Rear switches';
        if (name==='Captain_LCD_reference') return 'Captain screen';
        if (name==='Encoder_reference') return 'Captain encoder';
        if (name==='cable_space') return 'Reserved cable space';
        return 'MIDI Captain';
    }
    const meshes=scene.parts.map(part=>({...part,x:bounds(part.vertices)}));
    const moving=meshes.filter(part=>part.group==='arm'||part.group.endsWith('moving'));
    const fixed=meshes.filter(part=>part.group==='fixed'||part.group==='reference_fixed');
    const cableY=p.captain_depth/2, cableBottom=p.base_shelf_thickness+p.base_foot_height+1;
    const cableVertices=[];
    for (const x of [-p.captain_width/2,p.captain_width/2])
        for (const y of [cableY,cableY+p.cable_corridor_depth])
            for (const z of [cableBottom,p.cable_corridor_top]) cableVertices.push([x,y,z]);
    fixed.push({name:'cable_space',group:'reference_fixed',vertices:cableVertices,
        faces:[[0,1,3],[0,3,2],[4,6,7],[4,7,5],[0,4,5],[0,5,1],
               [2,3,7],[2,7,6],[0,2,6],[0,6,4],[1,5,7],[1,7,3]],x:bounds(cableVertices)});
    const pairs=[];
    for (const part of moving) {
        part.profile=hull(part.vertices.map(v=>[v[1],v[2]]));
        const direction=part.group==='arm'?(part.name.endsWith('_right')?1:-1):0;
        part.releaseDirection=direction;
        const shiftMin=Math.min(0,direction*releaseLimit), shiftMax=Math.max(0,direction*releaseLimit);
        for (const obstacle of fixed) {
            if (part.name.startsWith('03_arm_') && obstacle.name.startsWith('01_base_')) continue;
            const minX=Math.max(part.x.min+shiftMin,obstacle.x.min),maxX=Math.min(part.x.max+shiftMax,obstacle.x.max);
            if (maxX-minX<=tolerance) continue;
            const movingProfile=project(part,minX-shiftMax,maxX-shiftMin);
            const fixedProfile=project(obstacle,minX,maxX);
            if (movingProfile.length>=3 && fixedProfile.length>=3) pairs.push({part,obstacle,movingProfile,fixedProfile});
        }
    }
    function transform(profile, part, armAngle, displayAngle) {
        const a=armAngle*Math.PI/180,d=displayAngle*Math.PI/180;
        const arm=part.group==='arm',theta=arm?-a:-d,c=Math.cos(theta),s=Math.sin(theta);
        const originY=arm?p.base_pivot_y:p.pivot_y,originZ=arm?p.base_pivot_z:p.pivot_z;
        const targetY=arm?p.base_pivot_y:p.base_pivot_y+p.arm_length*Math.sin(a);
        const targetZ=arm?p.base_pivot_z:p.base_pivot_z+p.arm_length*Math.cos(a);
        return profile.map(v=>{const y=v[0]-originY,z=v[1]-originZ;return [targetY+c*y-s*z,targetZ+s*y+c*z];});
    }
    function overlaps(a,b) {
        for (const polygon of [a,b]) {
            for (let i=0;i<polygon.length;i++) {
                const v=polygon[i],w=polygon[(i+1)%polygon.length],dy=w[0]-v[0],dz=w[1]-v[1],length=Math.hypot(dy,dz);
                if (length<1e-8) continue;
                const axis=[-dz/length,dy/length],pa=a.map(q=>q[0]*axis[0]+q[1]*axis[1]),pb=b.map(q=>q[0]*axis[0]+q[1]*axis[1]);
                if (Math.min(Math.max(...pa),Math.max(...pb))-Math.max(Math.min(...pa),Math.min(...pb))<=tolerance) return false;
            }
        }
        return true;
    }
    function check(armAngle,displayAngle,release=0) {
        const contacts=[],seen=new Set();
        function record(part,obstacle,label) {
            if (!seen.has(label)) {seen.add(label);contacts.push({part:part.name,obstacle,label});}
        }
        for (const part of moving) {
            const profile=transform(part.profile,part,armAngle,displayAngle);
            if (Math.min(...profile.map(v=>v[1])) < -tolerance) record(part,'floor',partLabel(part)+' / floor');
        }
        for (const pair of pairs) {
            const {part,obstacle,movingProfile,fixedProfile}=pair,dx=part.releaseDirection*release;
            if (Math.min(part.x.max+dx,obstacle.x.max)-Math.max(part.x.min+dx,obstacle.x.min)<=tolerance) continue;
            if (overlaps(transform(movingProfile,part,armAngle,displayAngle),fixedProfile))
                record(part,obstacle.name,partLabel(part)+' / '+partLabel(obstacle));
        }
        return {status:contacts.length?'possible_contact':'no_contact_detected',contacts,method};
    }
    return {check};
}
