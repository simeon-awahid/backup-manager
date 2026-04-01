            "policy_type": "compliance",
            "policy_count": len(policies),
            "policies": policies,
            "exported_at": export_record["exported_at"]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to export compliance policies: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/policies/export-all")
async def export_all_policies():
    """Export all policy types"""
    try:
        graph_client = await get_graph_client()
        
        results = {}
        
        # Device Configuration
        device_config = await graph_client.get_device_configuration_policies()
        results["device_configuration"] = {
            "count": len(device_config),
            "policies": device_config
        }
        
        # Configuration Policies
        config = await graph_client.get_configuration_policies()
        results["configuration"] = {
            "count": len(config),
            "policies": config
        }
        
        # Conditional Access
        ca = await graph_client.get_conditional_access_policies()
        results["conditional_access"] = {
            "count": len(ca),
            "policies": ca
        }
        
        # Compliance
        compliance = await graph_client.get_compliance_policies()
        results["compliance"] = {
            "count": len(compliance),
            "policies": compliance
        }
        
        # Store all exports
        export_id = str(uuid.uuid4())
        export_record = {
            "id": export_id,
            "policy_type": "all",
            "policy_count": sum([r["count"] for r in results.values()]),
            "policies": results,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "synced_to_devops": False
        }
        await db.exports.insert_one(export_record)
        
        return {
            "export_id": export_id,
            "policy_type": "all",
            "total_count": export_record["policy_count"],
            "breakdown": {k: v["count"] for k, v in results.items()},
            "exported_at": export_record["exported_at"]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to export all policies: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Export History Endpoints
@api_router.get("/exports")
async def get_exports():
    """Get export history"""
    exports = await db.exports.find({}, {"_id": 0, "policies": 0}).sort("exported_at", -1).to_list(100)
    return {"exports": exports, "count": len(exports)}

@api_router.get("/exports/{export_id}")
async def get_export(export_id: str):
    """Get specific export details"""
    export = await db.exports.find_one({"id": export_id}, {"_id": 0})
    if not export:
        raise HTTPException(status_code=404, detail="Export not found")
    return export

@api_router.delete("/exports/{export_id}")
async def delete_export(export_id: str):
    """Delete an export record"""
    result = await db.exports.delete_one({"id": export_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Export not found")
    return {"success": True, "message": "Export deleted"}

# DevOps Sync Endpoints
@api_router.post("/devops/sync")
async def sync_to_devops(request: DevOpsSyncRequest):
    """Sync an export to Azure DevOps"""
    try:
        # Get export record
        export = await db.exports.find_one({"id": request.export_id}, {"_id": 0})
        if not export:
            raise HTTPException(status_code=404, detail="Export not found")
        
        devops_client = await get_devops_client()
        
        policy_type = export["policy_type"]
        timestamp = export["exported_at"].replace(":", "-").replace(".", "-")
        
        if policy_type == "all":
            # Push each policy type as separate file
            commit_message = request.commit_message or f"Export all policies - {timestamp}"
            results = []
            
            for ptype, pdata in export["policies"].items():
                file_path = f"/policies/{ptype}/{timestamp}.json"
                content = json.dumps(pdata["policies"], indent=2)
                result = await devops_client.push_file(file_path, content, f"Add {ptype} policies")
                results.append(result)
            
            # Update export record
            commit_id = results[-1].get("commits", [{}])[0].get("commitId", "unknown")
            await db.exports.update_one(
                {"id": request.export_id},
                {"$set": {"synced_to_devops": True, "devops_commit_id": commit_id}}
            )
            
            return {
                "success": True,
                "commit_id": commit_id,
                "message": "Successfully synced all policies to Azure DevOps"
            }
        else:
            # Single policy type
            file_path = f"/policies/{policy_type}/{timestamp}.json"
            content = json.dumps(export["policies"], indent=2)
            commit_message = request.commit_message or f"Export {policy_type} policies - {timestamp}"
            
            result = await devops_client.push_file(file_path, content, commit_message)
            commit_id = result.get("commits", [{}])[0].get("commitId", "unknown")
            
            # Update export record
            await db.exports.update_one(
                {"id": request.export_id},
                {"$set": {"synced_to_devops": True, "devops_commit_id": commit_id}}
            )
            
            return {
                "success": True,
                "commit_id": commit_id,
                "message": f"Successfully synced {policy_type} policies to Azure DevOps"
            }
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to sync to DevOps: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Dashboard Stats
@api_router.get("/dashboard/stats")
async def get_dashboard_stats():
    """Get dashboard statistics"""
    total_exports = await db.exports.count_documents({})
    synced_exports = await db.exports.count_documents({"synced_to_devops": True})
    
    # Get recent exports
    recent_exports = await db.exports.find({}, {"_id": 0, "policies": 0}).sort("exported_at", -1).to_list(5)
    
    # Count by policy type
    pipeline = [
        {"$group": {"_id": "$policy_type", "count": {"$sum": 1}}}
    ]
    type_counts = await db.exports.aggregate(pipeline).to_list(10)
    policy_type_counts = {item["_id"]: item["count"] for item in type_counts}
    
    settings = await get_settings()
    
    # Check if all required Azure AD credentials are configured
    azure_configured = bool(
        settings and 
        settings.azure_tenant_id and 
        settings.azure_client_id and 
        settings.azure_client_secret
    )
    
    # Check if all required DevOps credentials are configured
    devops_configured = bool(
        settings and 
        settings.devops_org and 
        settings.devops_project and 
        settings.devops_repo and 
        settings.devops_pat
    )
    
    # Check if GitHub baseline is configured
    github_configured = bool(
        settings and 
        settings.github_repo_url
    )
    
    return {
        "total_exports": total_exports,
        "synced_exports": synced_exports,
        "pending_sync": total_exports - synced_exports,
        "policy_type_counts": policy_type_counts,
        "recent_exports": recent_exports,
        "azure_configured": azure_configured,
        "devops_configured": devops_configured,
        "github_configured": github_configured
    }


# ============== GitHub CIS Baseline Client ==============

class GitHubClient:
    def __init__(self, repo_url: str, branch: str = "main", pat: Optional[str] = None):
        self.repo_url = repo_url  # format: "owner/repo"
        self.branch = branch
        self.pat = pat
        self.api_base = "https://api.github.com"
        
    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "MS-Policy-Manager"
        }
        if self.pat:
            headers["Authorization"] = f"token {self.pat}"
        return headers
    
    async def get_repo_contents(self, path: str = "") -> List[Dict]:
        """Get contents of a directory in the repo"""
        url = f"{self.api_base}/repos/{self.repo_url}/contents/{path}"
        params = {"ref": self.branch}
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._get_headers(), params=params)
            response.raise_for_status()
            return response.json()
    
    async def get_file_content(self, path: str) -> str:
        """Get content of a specific file"""
        url = f"{self.api_base}/repos/{self.repo_url}/contents/{path}"
        params = {"ref": self.branch}
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._get_headers(), params=params)
            response.raise_for_status()
            data = response.json()
            
            if data.get("encoding") == "base64":
                import base64
                return base64.b64decode(data["content"]).decode("utf-8")
            return data.get("content", "")
    
    async def get_all_json_files(self, path: str = "") -> List[Dict[str, Any]]:
        """Recursively get all JSON files from a path"""
        all_files = []
        
        try:
            contents = await self.get_repo_contents(path)
            
            if not isinstance(contents, list):
                contents = [contents]
            
            for item in contents:
                if item["type"] == "file" and item["name"].endswith(".json"):
                    try:
                        content = await self.get_file_content(item["path"])
                        json_data = json.loads(content)
                        all_files.append({
                            "path": item["path"],
                            "name": item["name"],
                            "data": json_data
                        })
                    except (json.JSONDecodeError, Exception) as e:
                        logger.warning(f"Failed to parse {item['path']}: {e}")
                elif item["type"] == "dir":
                    sub_files = await self.get_all_json_files(item["path"])
                    all_files.extend(sub_files)
                    
        except Exception as e:
            logger.error(f"Error fetching from GitHub: {e}")
            raise
            
        return all_files


async def get_github_client() -> GitHubClient:
    """Get GitHub client from stored settings"""
    settings = await get_settings()
    if not settings or not settings.github_repo_url:
        raise HTTPException(status_code=400, detail="GitHub repository not configured. Please update settings.")
    return GitHubClient(
        settings.github_repo_url,
        settings.github_branch or "main",
        settings.github_pat
    )


def compare_policies(tenant_policies: List[Dict], baseline_policies: List[Dict]) -> Dict:
    """
    Compare tenant policies with CIS baseline.
    Returns 4 categories: tenant_only, baseline_only, conflicting, matching
    """
    
    def get_policy_key(policy: Dict) -> str:
        """Generate a unique key for policy comparison"""
        # Try different common identifiers
        if "displayName" in policy:
            return policy["displayName"]
        if "name" in policy:
            return policy["name"]
        if "id" in policy:
            return policy["id"]
        return json.dumps(policy, sort_keys=True)[:100]
    
    def normalize_policy(policy: Dict) -> Dict:
        """Remove volatile fields for comparison"""
        excluded_keys = {"id", "@odata.type", "createdDateTime", "lastModifiedDateTime", 
                        "version", "createdBy", "lastModifiedBy", "roleScopeTagIds"}
        return {k: v for k, v in policy.items() if k not in excluded_keys}
    
    # Build lookup dictionaries
    tenant_by_name = {}
    for p in tenant_policies:
        key = get_policy_key(p)
        tenant_by_name[key] = p
    
    baseline_by_name = {}
    for p in baseline_policies:
        key = get_policy_key(p)
        baseline_by_name[key] = p
    
    tenant_keys = set(tenant_by_name.keys())
    baseline_keys = set(baseline_by_name.keys())
    
    # Calculate differences
    tenant_only_keys = tenant_keys - baseline_keys
    baseline_only_keys = baseline_keys - tenant_keys
    common_keys = tenant_keys & baseline_keys
    
    matching = []
    conflicting = []
    
    for key in common_keys:
        tenant_normalized = normalize_policy(tenant_by_name[key])
        baseline_normalized = normalize_policy(baseline_by_name[key])
        
        if tenant_normalized == baseline_normalized:
            matching.append({
                "name": key,
                "tenant": tenant_by_name[key],
                "baseline": baseline_by_name[key]
            })
        else:
            # Find specific differences
            differences = []
            all_keys = set(tenant_normalized.keys()) | set(baseline_normalized.keys())
            for k in all_keys:
                t_val = tenant_normalized.get(k)
                b_val = baseline_normalized.get(k)
                if t_val != b_val:
                    differences.append({
                        "field": k,
                        "tenant_value": t_val,
                        "baseline_value": b_val
                    })
            
            conflicting.append({
                "name": key,
                "tenant": tenant_by_name[key],
                "baseline": baseline_by_name[key],
                "differences": differences
            })
    
    tenant_only = [{"name": k, "policy": tenant_by_name[k]} for k in tenant_only_keys]
    baseline_only = [{"name": k, "policy": baseline_by_name[k]} for k in baseline_only_keys]
    
    return {
        "tenant_only": tenant_only,
        "baseline_only": baseline_only,
        "conflicting": conflicting,
        "matching": matching,
        "summary": {
            "tenant_only_count": len(tenant_only),
            "baseline_only_count": len(baseline_only),
            "conflicting_count": len(conflicting),
            "matching_count": len(matching)
        }
    }


# ============== CIS Baseline Comparison Endpoints ==============

@api_router.get("/baseline/files")
async def get_baseline_files():
    """Get list of baseline files from GitHub"""
    try:
        github_client = await get_github_client()
        settings = await get_settings()
        path = settings.github_baseline_path.strip("/") if settings.github_baseline_path else ""
        
        files = await github_client.get_all_json_files(path)
        
        return {
            "files": [{"path": f["path"], "name": f["name"]} for f in files],
            "count": len(files)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch baseline files: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/baseline/compare")
async def compare_with_baseline(policy_type: str = "all"):
    """
    Compare tenant policies with CIS baseline from GitHub.
    Returns 4 categories: tenant_only, baseline_only, conflicting, matching
    """
    try:
        # Get tenant policies
        graph_client = await get_graph_client()
        
        tenant_policies = []
        if policy_type in ["all", "device_configuration"]:
            policies = await graph_client.get_device_configuration_policies()
            tenant_policies.extend(policies)
        if policy_type in ["all", "configuration"]:
            policies = await graph_client.get_configuration_policies()
            tenant_policies.extend(policies)
        if policy_type in ["all", "conditional_access"]:
            policies = await graph_client.get_conditional_access_policies()
            tenant_policies.extend(policies)
        if policy_type in ["all", "compliance"]:
            policies = await graph_client.get_compliance_policies()
            tenant_policies.extend(policies)
        
        # Get baseline policies from GitHub
        github_client = await get_github_client()
        settings = await get_settings()
        path = settings.github_baseline_path.strip("/") if settings.github_baseline_path else ""
        
        baseline_files = await github_client.get_all_json_files(path)
        
        # Extract policies from baseline files
        baseline_policies = []
        for file in baseline_files:
            data = file["data"]
            if isinstance(data, list):
                baseline_policies.extend(data)
            elif isinstance(data, dict):
                # Could be a single policy or a wrapped response
                if "value" in data:
                    baseline_policies.extend(data["value"])
                else:
                    baseline_policies.append(data)
        
        # Compare
        comparison = compare_policies(tenant_policies, baseline_policies)
        comparison["tenant_total"] = len(tenant_policies)
        comparison["baseline_total"] = len(baseline_policies)
        comparison["compared_at"] = datetime.now(timezone.utc).isoformat()
        
        return comparison
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to compare with baseline: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/settings/test-github")
async def test_github_connection():
    """Test GitHub connection"""
    try:
        github_client = await get_github_client()
        settings = await get_settings()
        path = settings.github_baseline_path.strip("/") if settings.github_baseline_path else ""
        
        # Try to list contents
        contents = await github_client.get_repo_contents(path)
        file_count = len([c for c in contents if c.get("type") == "file" and c.get("name", "").endswith(".json")])
        
        return {
            "success": True, 
            "message": f"GitHub connection successful. Found {len(contents)} items, {file_count} JSON files in path."
        }
    except HTTPException as e:
        return {"success": False, "message": str(e.detail)}
    except Exception as e:
        return {"success": False, "message": str(e)}

# Include the router
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()

