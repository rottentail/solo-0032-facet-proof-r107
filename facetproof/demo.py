import argparse
import json
from pathlib import Path
import tempfile
import shutil
from .application import Application
from .errors import Fault, require
from .routing import route_request, PREFIX

FLOWS = ["mesh-intake", "mesh-retirement", "assembly-edit", "lens-edit", "seal-closure", "exposure-submit", "render-plates", "cancel-exposure", "retry-exposure", "verify-plate", "pixel-pick", "compare-plates", "proof-decision", "quarantine-plate", "recover-lease", "export-bundle", "reserve-capacity", "settle-capacity", "resize-capacity", "tile-progress", "offline-inspect", "backup-closure", "content-inspection", "display-review", "display-concurrency", "legacy-plate"]

def commission(root, flow, output=None):
    app = Application(root)
    def call(method, route, data=None): return route_request(app, method, PREFIX + route, data)
    def rejected(method, route, data, code):
        try: call(method, route, data)
        except Fault as exc:
            require(exc.code == code, "commission", f"Expected {code}, got {exc.code}", 500)
            return exc.code
        raise Fault("commission", "Expected rejection", 500)
    mesh_data = {"name":"斜翼", "vertices":[[-1,-1,0],[1,-1,0],[1,1,0],[-1,1,0],[0,0,1.2]], "faces":[[0,1,4],[1,2,4],[2,3,4],[3,0,4],[0,3,2],[0,2,1]]}
    mesh = call("POST", "meshes", mesh_data)
    pigment = call("POST", "pigments", {"name":"釉青", "color":[.12,.62,.61], "two_sided":True})
    assembly = call("POST", "assemblies", {"name":"斜翼结构说明图", "lens":{"eye":[3,2,4]}})
    assembly = call("PUT", "assemblies/"+assembly["id"], {"revision":1, "placements":[{"key":"wing", "mesh_id":mesh["id"], "pigment_id":pigment["id"], "transform":{"euler":[0,20,0]}}]})
    result = {"workflow":flow}
    if flow == "mesh-intake":
        result["rejected"] = rejected("POST", "meshes", {**mesh_data, "faces":[[0,0,1]]}, "face")
        return result
    if flow == "mesh-retirement":
        call("POST", "meshes/"+mesh["id"]+"/retire", {"revision":1})
        result["rejected"] = rejected("POST", "assemblies/"+assembly["id"]+"/seal", {"revision":2}, "retired")
        return result
    if flow in ("assembly-edit", "lens-edit"):
        changes = {"name":"更新说明图"} if flow == "assembly-edit" else {"lens":{"projection":"orthographic"}}
        edited = call("PUT", "assemblies/"+assembly["id"], {"revision":2, **changes})
        result.update(revision=edited["revision"], rejected=rejected("PUT", "assemblies/"+assembly["id"], {"revision":2, "name":"过期"}, "stale_revision"))
        return result
    seal = call("POST", "assemblies/"+assembly["id"]+"/seal", {"revision":2})
    if flow == "seal-closure":
        call("PUT", "pigments/"+pigment["id"], {"revision":1, "name":"改色", "color":[1,0,0]})
        require(call("GET", "seals/"+seal["id"]) == seal, "commission", "Seal changed", 500)
        result["rejected"] = rejected("PUT", "assemblies/"+assembly["id"], {"revision":3, "name":"改稿"}, "sealed")
        return result
    request = {"key":"commission", "width":96, "height":96, "samples":2}
    if flow in {'reserve-capacity', 'settle-capacity', 'resize-capacity'}:
        request.update(width=16, height=16, samples=1)
        call('PUT', 'resources', {'capacity':256, 'revision':1})
        exposure = call('POST', 'seals/'+seal['id']+'/exposures', request)
        result['reserved'] = call('GET', 'resources')['reserved']
        require(result['reserved'] == 256, 'commission', 'Reservation missing', 500)
        result['rejected'] = rejected('POST', 'seals/'+seal['id']+'/exposures', {**request,'key':'too-many'}, 'capacity_reserved')
        if flow == 'reserve-capacity':
            call('POST', 'exposures/'+exposure['id']+'/cancel')
        elif flow == 'settle-capacity':
            claim = app.exposures.claim()
            call('POST', 'exposures/'+exposure['id']+'/cancel')
            require(call('GET', 'resources')['reserved'] == 256, 'commission', 'Accepted cancellation released running work', 500)
            app.exposures.fail(exposure['id'], claim['token'], 'cancel_checkpoint')
        else:
            call('PUT', 'resources', {'capacity':512, 'revision':2})
            second = call('POST', 'seals/'+seal['id']+'/exposures', {**request,'key':'second'})
            policy = call('PUT', 'resources', {'capacity':256, 'revision':3})
            require(policy['overcommitted'] == 256, 'commission', 'Prior promises were erased', 500)
            rejected('PUT', 'resources', {'capacity':512,'revision':3}, 'stale_revision')
            require(call('POST', 'worker/step')['state']=='succeeded','commission','Promised work lost',500)
            require(call('POST', 'worker/step')['state']=='succeeded','commission','Second promise lost',500)
            result['settled'] = second['id']
        require(call('GET', 'resources')['reserved'] == 0, 'commission', 'Settled work leaked capacity', 500)
        result['capacity'] = call('GET', 'resources')
        return result
    exposure = call("POST", "seals/"+seal["id"]+"/exposures", request)
    eroute = "exposures/"+exposure["id"]
    if flow == 'tile-progress':
        require(not call('GET', eroute+'/progress')['planned'], 'commission', 'Queued work claims completed pixels', 500)
    if flow == "exposure-submit":
        require(call("POST", "seals/"+seal["id"]+"/exposures", request)["id"] == exposure["id"], "commission", "Idempotency mismatch", 500)
        result["rejected"] = rejected("POST", "seals/"+seal["id"]+"/exposures", {**request, "width":128}, "key_conflict")
        return result
    if flow == "cancel-exposure":
        result["state"] = call("POST", eroute+"/cancel")["state"]
        require(call("POST", "worker/step")["state"] == "idle", "commission", "Cancelled work claimed", 500)
        result["rejected"] = rejected("POST", eroute+"/retry", {}, "state")
        return result
    if flow in ("retry-exposure", "recover-lease"):
        claimed = app.exposures.claim(lease_seconds=-1 if flow == "recover-lease" else 120)
        if flow == "retry-exposure":
            app.exposures.fail(exposure["id"], claimed["token"], "interrupted")
            call("POST", eroute+"/retry")
        else:
            require(exposure["id"] in call("POST", "worker/recover")["recovered"], "commission", "Recovery failed", 500)
    rendered = call("POST", "worker/step")
    require(rendered["state"] == "succeeded", "commission", str(rendered), 500)
    plate = rendered["plate"]
    route = "plates/"+plate["id"]
    require(plate["payload"]["statistics"]["covered_pixels"] > 0, "commission", "Empty image", 500)
    result["statistics"] = plate["payload"]["statistics"]
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        for name in ("color.png", "depth.png", "object.png"): (Path(output)/name).write_bytes(call("GET", route+"/passes/"+name)[0])
    if flow == 'offline-inspect':
        from facetreader import validate_bundle
        from facetreader.light import decode_light as reader_light, tone_samples
        bundle = call('GET', route+'/bundle')[0]
        report = validate_bundle(bundle)
        require(report['valid'], 'commission', str(report), 500)
        damaged = validate_bundle(bundle[:-20])
        require(not damaged['valid'], 'commission', 'Truncated package was accepted', 500)
        # Recompute a ceiling display from the stored light with reader code
        # only, and require it to match the service's own conversion.
        window = (report.get('light') or {}).get('windows') or {}
        ceiling = window.get('ceiling') if isinstance(window, dict) else None
        members = {}
        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        if 'passes/light.hdr6' in members:
            decoded = reader_light(members['passes/light.hdr6'], report['light'],
                                   report['summary']['width'], report['summary']['height'])
            derived = tone_samples(decoded['samples'], window='ceiling', ceiling=ceiling)
            service = call('POST', route+'/bind', {'window': 'ceiling'})
            served = call('GET', 'displays/'+service['id']+'/rgb')[0]
            require(derived == served, 'commission', 'Offline re-tone disagrees with service display', 500)
            result['offline_retone'] = {'matches_service': True, 'window': 'ceiling',
                                        'light_status': report['light']['format']}
        result['offline'] = report['summary']
        result['rejected'] = damaged['error']
    elif flow == 'backup-closure':
        proof = call('POST', route+'/proofs', {'note':'备份前核验'})
        call('POST', 'proofs/'+proof['id']+'/decide', {'state':'accepted','note':'通道完整'})
        backup = call('POST', 'maintenance/backups')
        op = backup['operation_id']
        require(call('GET', 'maintenance/backups/'+op)['state']=='running','commission','Missing running backup',500)
        rejected('GET', 'maintenance/backups/'+op+'/verify', {}, 'backup_incomplete')
        completed = call('POST', 'maintenance/backups/'+op+'/resume')
        require(completed['state']=='succeeded','commission','Backup was not published',500)
        call('POST', route+'/quarantine', {'reason':'备份时点后的撤回'})
        require(call('GET', 'maintenance/backups/'+op+'/verify')['valid'],'commission','Immutable backup verification failed',500)
        restore_root = Path(root)/'restore-check'
        shutil.copytree(completed['data_directory'], restore_root)
        restored = Application(restore_root)
        require(restored.plates.verify(plate['id'])['valid'] and restored.proofs.get(proof['id'])['state']=='accepted',
                'commission','Restored snapshot mixed later review facts',500)
        require(restored.bundles.export(plate['id']), 'commission', 'Restored export failed', 500)
        result['backup'] = {'state':completed['state'],'references':len(completed['manifest']['closure']['references'])}
        result['rejected'] = rejected('GET', route+'/bundle', {}, 'plate_state')
    elif flow == 'content-inspection':
        result['inspection'] = call('GET', 'maintenance/inspect')
        result['rejected'] = rejected('GET', 'maintenance/backups/unknown', {}, 'backup_id')
    elif flow == 'tile-progress':
        progress = call('GET', eroute+'/progress')
        require(progress['complete'] and progress['committed_tiles'] == 9 and progress['state'] == 'succeeded',
                'commission', 'Persisted progress does not match the completed exposure', 500)
        result['progress'] = progress
        result['verification'] = call('POST', route+'/verify')
    elif flow == "verify-plate": result["verification"] = call("POST", route+"/verify")
    elif flow == "pixel-pick":
        result["pick"] = call("GET", route+"/pick?x=48&y=48")
        require(result["pick"]["object"] is not None, "commission", "Center object missing", 500)
    elif flow == "compare-plates":
        call("POST", "seals/"+seal["id"]+"/exposures", {**request, "key":"second"})
        second = call("POST", "worker/step")["plate"]
        result["comparison"] = call("POST", "comparisons", {"left":plate["id"], "right":second["id"]})["payload"]
        require(result["comparison"]["scene"]["object_changed"] == 0, "commission", "Determinism failed", 500)
        require(result["comparison"]["verdicts"]["scene_changed"] is False, "commission", "Identical scene reported changed", 500)
    elif flow in ("proof-decision", "quarantine-plate"):
        proof = call("POST", route+"/proofs", {"note":"检查结构轮廓"})
        result["proof"] = call("POST", "proofs/"+proof["id"]+"/decide", {"state":"accepted", "note":"通过"})["state"]
        if flow == "quarantine-plate":
            call("POST", route+"/quarantine", {"reason":"撤回"})
            require(call("GET", "proofs/"+proof["id"])["state"] == "invalidated", "commission", "Proof remains accepted", 500)
            call("POST", route+"/purge")
            result["rejected"] = rejected("GET", route+"/bundle", {}, "plate_state")
    elif flow == "export-bundle":
        bundle = call("GET", route+"/bundle")[0]
        require(bundle == call("GET", route+"/bundle")[0], "commission", "Bundle changed", 500)
        result["bundle_bytes"] = len(bundle)
    elif flow == "display-review":
        # A curved surface lit above one unit: the highlight has a smooth
        # range of brightness, which is exactly where an 8-bit display clips.
        import math
        segments = 60
        vertices, faces = [], []
        for index in range(segments+1):
            angle = 2*math.pi*index/segments
            vertices.append([math.cos(angle), math.sin(angle), 1])
            vertices.append([math.cos(angle), math.sin(angle), -1])
        for index in range(segments):
            a = index*2
            faces += [[a, a+2, a+1], [a+1, a+2, a+3]]
        cylinder = call("POST", "meshes", {"name": "高光圆柱", "vertices": vertices, "faces": faces})
        paint = call("POST", "pigments", {"name": "亮釉", "color": [.95, .95, .95],
                                          "two_sided": True, "roughness": .2, "specular": 1.0})
        bright = call("POST", "assemblies", {"name": "高光演示",
            "lens": {"eye": [0, -6, 0], "target": [0, 0, 0], "up": [0, 0, 1]},
            "illumination": {"lights": [{"direction": [.2, -1, .3], "intensity": 3.0, "color": [1, 1, 1]}],
                             "ambient": .02, "background": [.03, .03, .03]}})
        call("PUT", "assemblies/"+bright["id"], {"revision": 1, "placements": [
            {"key": "bright", "mesh_id": cylinder["id"], "pigment_id": paint["id"]}]})
        bright_seal = call("POST", "assemblies/"+bright["id"]+"/seal", {"revision": 2})
        call("POST", "seals/"+bright_seal["id"]+"/exposures",
             {"key": "bright", "width": 96, "height": 96, "samples": 1})
        lit = call("POST", "worker/step")["plate"]
        lit_route = "plates/"+lit["id"]
        light = lit["payload"]["display"]["lighting"]
        require(light["above_one_pixels"] > 0, "commission", "Scene has no over-one light", 500)
        bind = lambda settings: call("POST", lit_route+"/bind", settings)
        nominal, ceiling = bind({}), bind({"window": "ceiling"})
        raised = bind({"window": "ceiling", "exposure": 1.6})
        raw = lambda view: call("GET", "displays/"+view["id"]+"/rgb")[0]
        plain = call("GET", lit_route+"/passes/color.rgb")[0]
        wide = call("GET", lit_route+"/passes/bright.rgb")[0]
        require(raw(nominal) == plain, "commission", "Nominal display disagrees with the committed color pass", 500)
        require(raw(ceiling) != raw(nominal), "commission", "Ceiling display is identical", 500)
        listed = call("GET", lit_route+"/displays")
        require(len(listed["items"]) == 3, "commission", "Derived displays were not retained", 500)
        # Look only at channels whose stored light exceeds one unit: those are
        # the ones a nominal display cannot separate.
        from facetproof.display import decode_light
        samples = decode_light(app.blobs.read(lit["payload"]["passes"]["light.hdr6"]["sha256"]))
        highlight = [index for index, value in enumerate(samples) if value > 1.0]
        # A display switch on one frozen plate is not a scene change.
        comparison = call("POST", "comparisons", {"left": lit["id"], "right": lit["id"],
                                                  "display": {"window": "ceiling"}})["payload"]
        require(comparison["verdicts"]["scene_changed"] is False,
                "commission", "Display switch reported as scene change", 500)
        result["display"] = {
            "above_one_pixels": light["above_one_pixels"],
            "highlight_channels": len(highlight),
            "distinct_highlight_levels": {
                "stored_light": len({round(samples[index], 6) for index in highlight}),
                "nominal_8bit": len({plain[index] for index in highlight}),
                "ceiling_8bit": len({raw(ceiling)[index] for index in highlight}),
                "bright_point_8bit": len({wide[index] for index in highlight})},
            "clipped_highlight_bytes": {
                "nominal_8bit": sum(1 for index in highlight if plain[index] == 255),
                "ceiling_8bit": sum(1 for index in highlight if raw(ceiling)[index] == 255),
                "bright_point_8bit": sum(1 for index in highlight if wide[index] == 255)},
            "quantization": lit["payload"]["display"]["quantization"],
            "views": {"nominal": nominal["settings"], "ceiling": ceiling["settings"],
                      "raised": raised["settings"]},
            "verdicts": comparison["verdicts"],
            "recomputed_geometry": nominal["recomputed_geometry"]}
        if output:
            # The three views of the same frozen light, for visual comparison.
            for label, view in (("nominal", nominal), ("ceiling", ceiling), ("raised", raised)):
                (Path(output)/("highlight."+label+".png")).write_bytes(
                    call("GET", "displays/"+view["id"]+"/png")[0])
        result["rejected"] = rejected("POST", lit_route+"/bind", {"window": "wider"}, "choice")
        return result
    elif flow == "display-concurrency":
        import threading, time
        # A parallel exposure must keep making progress while review displays
        # are converted, and its cancellation must still land promptly.
        parallel = call("POST", "seals/"+seal["id"]+"/exposures",
                        {"key": "parallel", "width": 320, "height": 320, "samples": 2})
        outcome, latencies = {}, []
        worker = threading.Thread(target=lambda: outcome.setdefault("step", call("POST", "worker/step")))
        worker.start()
        time.sleep(.1)
        stop = [False]
        def switch():
            while not stop[0]:
                began = time.monotonic()
                call("POST", route+"/bind", {"window": "ceiling"})
                latencies.append(time.monotonic()-began)
        switchers = [threading.Thread(target=switch) for _ in range(3)]
        for thread in switchers: thread.start()
        time.sleep(.2)
        cancel = call("POST", "exposures/"+parallel["id"]+"/cancel")
        stop[0] = True
        for thread in switchers: thread.join()
        worker.join()
        state = call("GET", "exposures/"+parallel["id"])["state"]
        require(state in {"cancelled", "succeeded"}, "commission", "Parallel exposure stuck", 500)
        require(outcome["step"]["state"] in {"cancelled", "succeeded"}, "commission",
                "Worker outcome disagrees with exposure", 500)
        require(state == outcome["step"]["state"], "commission", "Cancellation and commit diverged", 500)
        require(len(latencies) > 0, "commission", "No display switch was attempted", 500)
        result["display_concurrency"] = {
            "binds_during_render": len(latencies),
            "max_bind_seconds": round(max(latencies), 3),
            "cancel_accepted": cancel["state"],
            "exposure_state": state,
            "worker_state": outcome["step"]["state"]}
        result["rejected"] = rejected("POST", eroute+"/cancel", {}, "state")
        return result
    elif flow == "legacy-plate":
        # A version-one plate stores only display bytes. It must remain
        # readable, but must not claim to have saved light it never had.
        from facetproof.values import canonical
        import io, json, zipfile
        bundle = call("GET", route+"/bundle")[0]
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        legacy = json.loads(members["plate.json"])
        for name in ("light.hdr6", "bright.rgb"):
            legacy["payload"]["passes"].pop(name)
        del legacy["payload"]["display"]
        legacy["payload"]["format"] = "facet-plate-1"
        from facetproof.values import digest
        legacy["payload"]["visual_hash"] = digest(legacy["payload"]["passes"])
        members.pop("light.json")
        members["plate.json"] = canonical(legacy).encode()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, raw in members.items():
                archive.writestr(name, raw)
        from facetreader import validate_bundle
        report = validate_bundle(buffer.getvalue())
        require(report["valid"] and report["light"]["status"] == "not_stored",
                "commission", "Legacy package was not readable as legacy", 500)
        # The same plate must refuse to pretend its light survived.
        legacy["payload"]["display"] = {"light_format": "facet-light-hdr6",
                                        "encoding": "light-hdr6", "bytes_per_pixel": 6}
        members["plate.json"] = canonical(legacy).encode()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, raw in members.items():
                archive.writestr(name, raw)
        forged = validate_bundle(buffer.getvalue())
        require(not forged["valid"] and forged["error"] == "display_metadata",
                "commission", "Forged stored-light claim was accepted", 500)
        # The service must also refuse to re-tone a plate with no light.
        stripped = {k: v for k, v in plate["payload"].items() if k != "display"}
        stripped["passes"] = {k: v for k, v in stripped["passes"].items()
                              if k not in ("light.hdr6", "bright.rgb")}
        with app.db.unit_of_work() as conn:
            conn.execute("UPDATE plates SET payload=? WHERE id=?", (canonical(stripped), plate["id"]))
        result["rejected"] = rejected("POST", route+"/bind", {}, "display_light_missing")
        result["legacy"] = {"offline_status": report["light"]["status"],
                            "forged_rejected": forged["error"]}
        return result
    if "rejected" not in result:
        result["rejected"] = rejected("POST", eroute+"/cancel", {}, "state")
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--flow", choices=FLOWS, default="render-plates")
    parser.add_argument("--output")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="facetproof-") as root:
        print(json.dumps(commission(root, args.flow, args.output), ensure_ascii=False, indent=2))
if __name__ == "__main__": main()
